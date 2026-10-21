import serial
import serial.tools.list_ports
import time
import os
from ecc_hamming import HammingECC
from nand_protocol import NandProtocol

# Import các thành phần giao diện từ thư viện rich
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table
from rich.panel import Panel

TOTAL_BLOCKS = 1024 
PAGES_PER_BLOCK = 64
BBT_PHYSICAL_BLOCK = 1023
BBT_PAGE_ID = BBT_PHYSICAL_BLOCK * PAGES_PER_BLOCK
MAGIC_HEADER = b"BBT!"

console = Console()

class PCNandManagerUI:
    def __init__(self):
        self.bbt_map = {}
        self.bad_blocks_list = []
        
        # Tự động dò tìm cổng COM của RP2040
        port = self.auto_detect_rp2040()
        
        try:
            self.ser = serial.Serial(port, baudrate=115200, timeout=5)
            time.sleep(1.5)
            self.ser.reset_input_buffer()
            self.protocol = NandProtocol(self.ser, timeout=10.0)
            self.protocol.get_info()
            console.print(f"[green]✓[/green] Đã kết nối thành công tới [cyan]{port}[/cyan]")
        except Exception as e:
            console.print(f"[red]❌ LỖI KHỞI ĐỘNG CỔNG KẾT NỐI:[/red] {e}")
            exit(1)

        self.initialize_bbt()

    def auto_detect_rp2040(self):
        """Quét toàn bộ cổng COM trên máy để tìm thiết bị chứa định danh RP2040"""
        console.print("[yellow]🔄 Đang quét tìm mạch RP2040 kết nối qua USB...[/yellow]")
        ports = list(serial.tools.list_ports.comports())
        detected_port = None
        
        # Cách 1: Tìm qua VID/PID hoặc chuỗi ký tự định danh phần cứng
        for p in ports:
            # VID của Raspberry Pi thường là 0x2E8A
            if p.vid == 0x2E8A or "Board in FS mode" in p.description or "Raspberry Pi" in p.description or "Pico" in p.description:
                detected_port = p.device
                console.print(f"[green]✓[/green] Tìm thấy thiết bị: [bold cyan]{p.description}[/bold cyan] trên [bold yellow]{p.device}[/bold yellow]")
                break
                
        # Cách 2: Dự phòng nếu driver Windows thay đổi tên mô tả, cho phép hiển thị danh sách chọn
        if not detected_port:
            if len(ports) == 0:
                console.print("[bold red]❌ LỖI:[/bold red] Không phát hiện bất kỳ cổng COM nào đang cắm trên máy tính!")
                console.print("[yellow]Vui lòng kiểm tra lại dây cáp USB và cổng cắm mạch RP2040.[/yellow]")
                exit(1)
                
            console.print("[yellow]⚠ Không thể tự động nhận diện chuỗi ký tự RP2040. Vui lòng chọn thủ công:[/yellow]")
            table = Table(title="Danh Sách Các Cổng COM Đang Khả Dụng")
            table.add_column("STT", style="cyan", justify="center")
            table.add_column("Cổng COM", style="yellow")
            table.add_column("Mô tả Thiết bị", style="green")
            
            for idx, p in enumerate(ports):
                table.add_row(str(idx + 1), p.device, p.description)
            console.print(table)
            
            while True:
                try:
                    choice = int(input(f"Nhập số thứ tự cổng COM của bạn (1-{len(ports)}): ").strip())
                    if 1 <= choice <= len(ports):
                        detected_port = ports[choice - 1].device
                        break
                except ValueError:
                    pass
                print("Lựa chọn không hợp lệ. Vui lòng nhập lại!")
                
        return detected_port

    def initialize_bbt(self):
        console.print(Panel("[bold yellow]HỆ THỐNG QUẢN LÝ KHỐI LỖI (BBMS)[/bold yellow]", expand=False))
        
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
            progress.add_task(description="Đang tìm kiếm Bảng BBT cố định trên chip...", total=None)
            loaded = self.load_bbt_from_nand()
            
        if loaded:
            console.print(" [green]✓[/green] Đã nạp thành công Bảng BBT cố định từ Block 1023 (Tốc độ cực nhanh).")
        else:
            console.print(" [yellow]⚠️ CẢNH BÁO:[/yellow] Không tìm thấy BBT hợp lệ. Bắt đầu quét diện rộng phần cứng...")
            self.scan_entire_nand()
            self.save_bbt_to_nand()
            
        table = Table(title="Thông Số Chip Hiện Tại", title_style="bold magenta")
        table.add_column("Thông số", style="cyan")
        table.add_column("Giá trị", style="green")
        table.add_row("Tổng số Block vật lý", str(TOTAL_BLOCKS))
        table.add_row("Block Logic khả dụng", str(len(self.bbt_map)))
        table.add_row("Số Bad Blocks bị loại bỏ", str(len(self.bad_blocks_list)))
        console.print(table)

    def scan_entire_nand(self):
        self.bad_blocks_list = []
        self.bbt_map = {}
        logical_counter = 0

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task("Quét Bad Block...", total=TOTAL_BLOCKS - 1)
            
            for p_block in range(TOTAL_BLOCKS - 1):
                if self.protocol.check_block(p_block):
                    self.bbt_map[logical_counter] = p_block
                    logical_counter += 1
                else: 
                    console.print(f"   [red]✖[/red] Phát hiện khối lỗi nhà máy tại [bold red]Physical Block {p_block}[/bold red]")
                    self.bad_blocks_list.append(p_block)
                    
                progress.update(task, advance=1)

    def save_bbt_to_nand(self):
        flat_bbt = bytearray(TOTAL_BLOCKS)
        for p_block in range(TOTAL_BLOCKS):
            if p_block in self.bad_blocks_list or p_block == BBT_PHYSICAL_BLOCK:
                flat_bbt[p_block] = 0x01
                
        pure_data = MAGIC_HEADER + bytes(flat_bbt)
        pure_data += b'\xFF' * (2048 - len(pure_data))
        
        ecc_bytes = b""
        for i in range(4):
            chunk = pure_data[i*512 : (i+1)*512]
            ecc_bytes += HammingECC.calculate_512(chunk)
            
        oob_area = b'\xFF' + ecc_bytes + b'\xFF' * (64 - 1 - len(ecc_bytes))
        full_packet = pure_data + oob_area
        
        self.protocol.erase_block(BBT_PHYSICAL_BLOCK)
        self.protocol.write_page(BBT_PAGE_ID, full_packet)
        if self.protocol.read_page(BBT_PAGE_ID) != full_packet:
            raise IOError("BBT read-back verification failed")
        console.print(" [green]✓[/green] Đã đồng bộ cố định bảng BBT mới xuống chip thành công.")
        return True

    def load_bbt_from_nand(self):
        raw_data = self.protocol.read_page(BBT_PAGE_ID)
        if len(raw_data) != 2112: return False
            
        pure_data = raw_data[0:2048]
        oob_area = raw_data[2048:2112]
        saved_ecc = oob_area[1:13]
        
        fixed_data = bytearray()
        for i in range(4):
            chunk = pure_data[i*512 : (i+1)*512]
            status, fixed_chunk = HammingECC.correct_512(chunk, saved_ecc[i*3:(i+1)*3], HammingECC.calculate_512(chunk))
            if status == -1: return False
            fixed_data.extend(fixed_chunk)
            
        if fixed_data[0:4] != MAGIC_HEADER: return False
            
        flat_bbt = fixed_data[4 : 4 + TOTAL_BLOCKS]
        self.bad_blocks_list = []
        self.bbt_map = {}
        logical_counter = 0
        
        for p_block, status_byte in enumerate(flat_bbt):
            if status_byte == 0x01:
                self.bad_blocks_list.append(p_block)
            else:
                self.bbt_map[logical_counter] = p_block
                logical_counter += 1
        return True

    def get_physical_block(self, logical_block_id):
        return self.bbt_map[logical_block_id]

    def read_logical_page_with_ecc(self, logical_block_id, page_offset):
        p_block = self.get_physical_block(logical_block_id)
        actual_page_id = (p_block * PAGES_PER_BLOCK) + page_offset
        
        raw_data = self.protocol.read_page(actual_page_id)
        
        pure_data = raw_data[0:2048]
        oob_area = raw_data[2048:2112]
        saved_ecc = oob_area[1:13]
        
        corrected_data = bytearray()
        uncorrectable_flag = False
        
        for i in range(4):
            chunk = pure_data[i*512 : (i+1)*512]
            status, fixed_chunk = HammingECC.correct_512(chunk, saved_ecc[i*3:(i+1)*3], HammingECC.calculate_512(chunk))
            corrected_data.extend(fixed_chunk)
            if status == 1:
                console.print(f"\n[bold yellow]⚠️ ECC:[/bold yellow] Đã tự động vá thành công 1 bit lỗi tại L_Block {logical_block_id}, Page {page_offset}, Phân đoạn {i}")
            elif status == -1:
                console.print(f"\n[bold red]❌ LỖI ECC NẶNG:[/bold red] L_Block {logical_block_id}, Page {page_offset} lỗi từ 2 bit trở lên! Không thể phục hồi.")
                uncorrectable_flag = True
                
        if uncorrectable_flag:
            return False, bytes(corrected_data)
        return True, bytes(corrected_data)

    def write_logical_page_with_ecc(self, logical_block_id, page_offset, pure_data_2048_bytes):
        ecc_bytes = b""
        for i in range(4):
            chunk = pure_data_2048_bytes[i*512 : (i+1)*512]
            ecc_bytes += HammingECC.calculate_512(chunk)
            
        oob_area = b'\xFF' + ecc_bytes + b'\xFF' * (64 - 1 - len(ecc_bytes))
        full_packet = pure_data_2048_bytes + oob_area
        
        p_block = self.get_physical_block(logical_block_id)
        actual_page_id = (p_block * PAGES_PER_BLOCK) + page_offset
        
