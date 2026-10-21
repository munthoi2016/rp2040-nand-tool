import os
import struct
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except Exception:
    serial = None

from nand_protocol import NandProtocol

try:
    from ecc_hamming import HammingECC
except Exception:
    class HammingECC:
        @staticmethod
        def calculate_512(chunk):
            return b"\x00" * 12

        @staticmethod
        def correct_512(chunk, saved_ecc, calc):
            return 0, bytes(chunk)

TOTAL_BLOCKS = 1024
PAGES_PER_BLOCK = 64
BBT_PHYSICAL_BLOCK = 1023
BBT_PAGE_ID = BBT_PHYSICAL_BLOCK * PAGES_PER_BLOCK
MAGIC_HEADER = b"BBT!"
BIN_MAGIC = b"TNAND"
BIN_VERSION = 1


class MockNandDevice:
    def __init__(self):
        self.page_size = 2048
        self.oob_size = 64
        self.pages_per_block = PAGES_PER_BLOCK
        self.total_blocks = TOTAL_BLOCKS
        self.storage = {}
        self.bad_blocks = {0, 1, 5, 17, 33, 101, 999}
        self._seed_initial_state()

    def _seed_initial_state(self):
        for page_id in range(self.total_blocks * self.pages_per_block):
            if page_id // self.pages_per_block in self.bad_blocks:
                continue
            if page_id == BBT_PAGE_ID:
                continue
            self.storage[page_id] = b"\xFF" * (self.page_size + self.oob_size)

    def read_page(self, page_id):
        if page_id < 0 or page_id >= self.total_blocks * self.pages_per_block:
            raise ValueError(f"Invalid page index: {page_id}")
        return self.storage.get(page_id, b"\xFF" * (self.page_size + self.oob_size))

    def write_page(self, page_id, full_packet):
        if len(full_packet) != self.page_size + self.oob_size:
            raise ValueError(f"Invalid page packet length: {len(full_packet)}")
        self.storage[page_id] = bytes(full_packet)
        return True

    def erase_block(self, block_id):
        for page_id in range(block_id * self.pages_per_block, (block_id + 1) * self.pages_per_block):
            self.storage.pop(page_id, None)
        return True

    def scan_bad_blocks(self):
        bbt = {}
        for block in range(TOTAL_BLOCKS - 1):
            if block in self.bad_blocks:
                continue
            bbt[len(bbt)] = block
        return bbt

    def check_block(self, block_id):
        return block_id not in self.bad_blocks


class NandFlashGuiApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NAND Flash SLC Tool - RP2040/RP2350 Bridge")
        self.geometry("840x620")
        self.minsize(780, 540)

        self.ser = None
        self.protocol = None
        self.mock_mode = False
        self.mock_device = MockNandDevice()
        self.bbt_map = {}
        self.bad_blocks_list = []
        self.is_processing = False
        self.board_info = "Unknown"
        self.selected_port = tk.StringVar(value="")

        self.configure(bg="#0f172a")
        self.create_widgets()
        self.after(200, self.auto_detect_com)

    def create_widgets(self):
        top = tk.Frame(self, bg="#111827", padx=16, pady=12)
        top.pack(fill="x")

        self.lbl_title = tk.Label(
            top,
            text="NAND Flash SLC Tool",
            fg="#f8fafc",
            bg="#111827",
            font=("Arial", 18, "bold"),
        )
        self.lbl_title.pack(side="left")

        self.lbl_hw = tk.Label(
            top,
            text="Hardware: Chưa kiểm tra",
            fg="#fbbf24",
            bg="#111827",
            font=("Arial", 10, "bold"),
        )
        self.lbl_hw.pack(side="right")

        status_bar = tk.Frame(self, bg="#1f2937", bd=1, relief="sunken")
        status_bar.pack(fill="x", padx=12, pady=(0, 10))

        self.lbl_status = tk.Label(
            status_bar,
            text="Trạng thái: Chưa kết nối phần cứng",
            fg="#facc15",
            bg="#1f2937",
            font=("Arial", 12, "bold"),
        )
        self.lbl_status.pack(side="left", padx=12, pady=10)

        port_frame = tk.Frame(self, bg="#0f172a")
        port_frame.pack(fill="x", padx=12, pady=(0, 8))

        self.cmb_ports = ttk.Combobox(port_frame, textvariable=self.selected_port, state="readonly", width=30)
        self.cmb_ports.pack(side="left", padx=(0, 8))

        self.btn_scan = tk.Button(port_frame, text="Quét & Kết nối", width=18, bg="#2563eb", fg="white", command=self.auto_detect_com)
        self.btn_scan.pack(side="left", padx=8)

        self.btn_mock = tk.Button(port_frame, text="Mock mode", width=14, bg="#7c3aed", fg="white", command=self.enable_mock_mode)
        self.btn_mock.pack(side="left", padx=8)

        info_frame = tk.LabelFrame(self, text="Thông tin chip", bg="#0f172a", fg="#e2e8f0", padx=12, pady=8)
        info_frame.pack(fill="x", padx=12, pady=(0, 8))

        self.lbl_total_blocks = tk.Label(info_frame, text=f"• Tổng số khối vật lý: {TOTAL_BLOCKS} Blocks", bg="#0f172a", fg="#f8fafc")
        self.lbl_total_blocks.grid(row=0, column=0, padx=(0, 20), pady=4, sticky="w")

        self.lbl_good_blocks = tk.Label(info_frame, text="• Khối Logic khả dụng (BBMS): -- Blocks", bg="#0f172a", fg="#f8fafc")
        self.lbl_good_blocks.grid(row=0, column=1, padx=(0, 20), pady=4, sticky="w")

        self.lbl_bad_blocks = tk.Label(info_frame, text="• Số khối hỏng (Bad Blocks): -- Blocks", bg="#0f172a", fg="#f8fafc")
        self.lbl_bad_blocks.grid(row=1, column=0, padx=(0, 20), pady=4, sticky="w")

        self.lbl_mode = tk.Label(info_frame, text="• Chế độ: Board / Mock", bg="#0f172a", fg="#f8fafc")
        self.lbl_mode.grid(row=1, column=1, padx=(0, 20), pady=4, sticky="w")

        self.btn_bbt_scan = tk.Button(
            info_frame,
            text="Quét Bad Block / khởi tạo BBT",
            bg="#475569",
            fg="white",
            command=self.start_bbt_scan,
        )
        self.btn_bbt_scan.grid(row=2, column=0, columnspan=2, pady=(6, 2), sticky="w")

        action = tk.Frame(self, bg="#0f172a")
        action.pack(fill="x", padx=12, pady=(0, 8))

        self.btn_dump = tk.Button(action, text="DUMP CHIP (Đọc -> File .bin)", width=28, height=2, bg="#16a34a", fg="white", command=self.start_dump_thread)
        self.btn_dump.grid(row=0, column=0, padx=12, pady=10, sticky="ew")

        self.btn_flash = tk.Button(action, text="FLASH CHIP (Ghi File -> Chip)", width=28, height=2, bg="#dc2626", fg="white", command=self.start_flash_thread)
        self.btn_flash.grid(row=0, column=1, padx=12, pady=10, sticky="ew")

        action.grid_columnconfigure(0, weight=1)
        action.grid_columnconfigure(1, weight=1)

        progress = tk.LabelFrame(self, text="Tiến độ", bg="#0f172a", fg="#e2e8f0", padx=10, pady=8)
        progress.pack(fill="x", padx=12, pady=(0, 8))

        self.lbl_progress_desc = tk.Label(progress, text="Hệ thống đang sẵn sàng xử lý...", bg="#0f172a", fg="#f8fafc")
        self.lbl_progress_desc.pack(anchor="w", padx=6, pady=(4, 2))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress, orient="horizontal", length=700, mode="determinate", variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", padx=6, pady=(0, 6))

        log_frame = tk.LabelFrame(self, text="Nhật ký hoạt động", bg="#0f172a", fg="#e2e8f0", padx=8, pady=8)
        log_frame.pack(fill="both", padx=12, pady=(0, 12), expand=True)

        self.txt_log = tk.Text(log_frame, height=15, bg="#020817", fg="#e2e8f0", font=("Consolas", 10))
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.insert(tk.END, "Phần mềm quản lý NAND Flash đã khởi tạo.\n")
        self.txt_log.see(tk.END)

        self.set_status("Trạng thái: Chưa kết nối phần cứng", "yellow")
        self.populate_ports()
        self.update_chip_info_ui()
        self.toggle_buttons(True)

    def log(self, text):
        self.txt_log.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.txt_log.see(tk.END)

    def safe_ui(self, func, *args):
        self.after(0, lambda: func(*args))

    def set_status(self, text, color):
        self.lbl_status.configure(text=text, fg=color)

    def populate_ports(self):
        if serial is None:
            self.cmb_ports["values"] = []
            self.cmb_ports.set("")
            self.selected_port.set("")
            return
        try:
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            ports = []
        self.cmb_ports["values"] = ports
        if ports:
            self.cmb_ports.set(ports[0])
            self.selected_port.set(ports[0])
        else:
            self.cmb_ports.set("")
            self.selected_port.set("")

    def toggle_buttons(self, state):
        self.is_processing = not state
        normal_state = "normal" if state else "disabled"
        self.btn_dump.configure(state=normal_state)
        self.btn_flash.configure(state=normal_state)
        self.btn_scan.configure(state=normal_state)
        self.btn_mock.configure(state=normal_state)
        self.btn_bbt_scan.configure(state=normal_state)

    def enable_mock_mode(self):
        self.mock_mode = True
        self.ser = None
        self.protocol = None
        self.set_status("Trạng thái: Chế độ Mock đang hoạt động", "purple")
        self.lbl_hw.configure(text="Hardware: Mock mode")
        self.lbl_mode.configure(text="• Chế độ: Mock")
        self.log("Chế độ mock đã được kích hoạt. Ứng dụng sẽ làm việc mà không cần board thật.")
        self.initialize_bbt()

    def initialize_bbt(self):
        try:
            if self.load_bbt_from_nand():
                self.log("Đã nạp BBT hiện có từ NAND.")
            elif self.mock_mode:
                self.bbt_map = self.mock_device.scan_bad_blocks()
                self.bad_blocks_list = sorted(self.mock_device.bad_blocks)
                self.log("Mock mode: đã tạo bản đồ block từ thiết bị giả lập.")
            else:
                self.bbt_map = {}
                self.bad_blocks_list = []
                self.log(
                    "Không tìm thấy BBT hợp lệ. Chưa tự động ghi/xóa NAND; "
                    "hãy dùng nút quét BBT sau khi xác nhận thiết bị."
                )
        except Exception as exc:
            self.bbt_map = {}
            self.bad_blocks_list = []
            self.log(f"Không thể khởi tạo BBT: {exc}")
        self.update_chip_info_ui()

    def start_bbt_scan(self):
        if not self.mock_mode and self.protocol is None:
            self.log("Không thể quét BBT: chưa kết nối NAND bridge.")
            return
        if not messagebox.askyesno(
            "Xác nhận quét NAND",
            "Quét marker bad-block trên toàn chip. Nếu quét thành công, "
            "BBT sẽ được ghi vào block dự trữ 1023 (block này sẽ bị xóa). Tiếp tục?",
        ):
            return
        threading.Thread(target=self.scan_and_save_bbt_thread, daemon=True).start()

    def serial_ports(self):
        if serial is None:
            return []
        try:
            return list(serial.tools.list_ports.comports())
        except Exception:
            return []

    def read_serial_until(self, expected_len, timeout=1.0):
        if self.ser is None:
            raise RuntimeError("Serial not initialized")
        start = time.time()
        chunks = []
        while time.time() - start < timeout:
            if self.ser.in_waiting > 0:
                data = self.ser.read(self.ser.in_waiting)
                chunks.append(data)
                if sum(len(x) for x in chunks) >= expected_len:
                    break
            time.sleep(0.01)
        out = b"".join(chunks)
        if len(out) < expected_len:
            raise TimeoutError(f"Timeout waiting for {expected_len} bytes, got {len(out)}")
        return out[:expected_len]

    def probe_device(self):
        if self.protocol is None:
            return False
        try:
            info = self.protocol.get_info()
            return (
                info["page_size"] == 2048
                and info["oob_size"] == 64
                and info["pages_per_block"] == PAGES_PER_BLOCK
                and info["total_blocks"] == TOTAL_BLOCKS
            )
        except Exception:
            return False

    def read_board_info(self):
        if self.protocol is None:
            return "Unknown"
        info = self.protocol.get_info()
        return (
            f"RP2040 NAND bridge v1 "
            f"({info['page_size']}+{info['oob_size']} bytes, "
            f"{info['pages_per_block']} pages/block)"
        )

    def connect_to_port(self, port_name):
        if serial is None:
            self.log("Thư viện pyserial chưa được cài đặt.")
            self.enable_mock_mode()
            return False
        try:
            if self.ser is not None:
                self.ser.close()
            self.ser = serial.Serial(port_name, baudrate=115200, timeout=0.5)
            self.protocol = NandProtocol(self.ser, timeout=10.0)
            time.sleep(0.2)
            self.ser.reset_input_buffer()
            if not self.probe_device():
                self.log("Board kết nối nhưng không trả lời handshake. Bỏ qua cổng này.")
                self.ser.close()
                self.ser = None
                self.protocol = None
                return False
            self.mock_mode = False
            self.board_info = self.read_board_info()
            self.set_status(f"Trạng thái: Đã kết nối ({port_name})", "green")
            self.lbl_hw.configure(text=f"Hardware: {self.board_info}")
            self.lbl_mode.configure(text="• Chế độ: Board")
            self.log(f"Kết nối COM thành công và board xác thực: {self.board_info}")
            return True
        except Exception as exc:
            self.log(f"Lỗi mở cổng COM {port_name}: {exc}")
            self.ser = None
            self.protocol = None
            self.enable_mock_mode()
            return False

    def auto_detect_com(self):
        self.log("Đang quét cổng COM...")
        ports = self.serial_ports()
        detected = None

        for p in ports:
            desc = str(getattr(p, "description", "")).lower()
            vid = getattr(p, "vid", None)
            if vid == 0x2E8A or "board in fs mode" in desc or "raspberry pi" in desc or "pico" in desc:
                detected = p.device
                self.log(f"Phát hiện board: {p.description} tại {p.device}")
                break

        if not detected and ports:
            detected = ports[0].device
            self.log(f"Không rõ tên thiết bị, dùng cổng đầu tiên: {detected}")

        if detected:
            self.selected_port.set(detected)
            self.cmb_ports.set(detected)
            if self.connect_to_port(detected):
                self.initialize_bbt()
                return

        self.log("Không tìm thấy board hợp lệ. Chuyển sang mock mode.")
        self.enable_mock_mode()

    def read_page(self, page_id):
        if self.mock_mode:
            return self.mock_device.read_page(page_id)
        if self.protocol is None:
            raise RuntimeError("Serial is not available")
        return self.protocol.read_page(page_id)

    def write_page(self, page_id, full_packet):
        if self.mock_mode:
            self.mock_device.write_page(page_id, full_packet)
            return True
        if self.protocol is None:
            raise RuntimeError("Serial is not available")
        self.protocol.write_page(page_id, full_packet)
        return True

    def erase_block(self, block_id):
        if self.mock_mode:
            self.mock_device.erase_block(block_id)
            return True
        if self.protocol is None:
            raise RuntimeError("Serial is not available")
        self.protocol.erase_block(block_id)
        return True

    def check_block(self, block_id):
        if self.mock_mode:
            return self.mock_device.check_block(block_id)
        if self.protocol is None:
            raise RuntimeError("Serial is not available")
        return self.protocol.check_block(block_id)

    def valid_page_data(self, raw_data):
        return isinstance(raw_data, (bytes, bytearray)) and len(raw_data) >= 2112

    def load_bbt_from_nand(self):
        try:
            if self.mock_mode:
                raw_data = self.mock_device.read_page(BBT_PAGE_ID)
            else:
                if self.protocol is None:
                    return False
                raw_data = self.protocol.read_page(BBT_PAGE_ID)
                if len(raw_data) != 2112:
                    return False

            pure_data = raw_data[0:2048]
            oob_area = raw_data[2048:2112]
            saved_ecc = oob_area[1:13]
            fixed_data = bytearray()

            for i in range(4):
                chunk = pure_data[i * 512 : (i + 1) * 512]
                status, fixed_chunk = HammingECC.correct_512(chunk, saved_ecc[i * 3 : (i + 1) * 3], HammingECC.calculate_512(chunk))
                if status == -1:
                    return False
                fixed_data.extend(fixed_chunk)

            if fixed_data[0:4] != MAGIC_HEADER:
                return False

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
        except Exception as exc:
            self.log(f"Lỗi đọc BBT qua NAND bridge: {exc}")
            raise

    def scan_and_save_bbt_thread(self):
        self.toggle_buttons(False)
        self.safe_ui(self.lbl_progress_desc.configure, text="Đang quét toàn bộ các khối phần cứng để tìm Bad Block...")

        self.bad_blocks_list = []
        self.bbt_map = {}
        logical_counter = 0
        scan_complete = True

        for p_block in range(TOTAL_BLOCKS - 1):
            try:
                if self.check_block(p_block):
                    self.bbt_map[logical_counter] = p_block
                    logical_counter += 1
                else:
                    self.log(f"Phát hiện Bad Block vật lý tại khối {p_block}")
                    self.bad_blocks_list.append(p_block)
            except Exception as exc:
                self.log(f"Lỗi trong quá trình quét khối {p_block}: {exc}")
                scan_complete = False
                break

            progress = ((p_block + 1) / (TOTAL_BLOCKS - 1)) * 100
            self.safe_ui(self.progress_var.set, progress)

        if scan_complete:
            self.log("Đang lưu bảng BBT vào block cố định 1023...")
            scan_complete = self.save_bbt_to_nand()
        if not scan_complete:
            self.log("Quét/lưu BBT chưa hoàn tất; không dùng bản đồ block chưa đầy đủ.")
            self.bbt_map = {}

        self.update_chip_info_ui()
        self.safe_ui(self.progress_var.set, 0)
        self.safe_ui(
            self.lbl_progress_desc.configure,
            text="Quét BBT hoàn tất." if scan_complete else "Quét BBT thất bại.",
        )
        self.toggle_buttons(True)

    def save_bbt_to_nand(self):
        try:
            flat_bbt = bytearray(TOTAL_BLOCKS)
            for block in range(TOTAL_BLOCKS):
                if block in self.bad_blocks_list or block == BBT_PHYSICAL_BLOCK:
                    flat_bbt[block] = 0x01

            pure_data = MAGIC_HEADER + bytes(flat_bbt)
            pure_data += b"\xFF" * (2048 - len(pure_data))

            ecc_bytes = b""
            for i in range(4):
                chunk = pure_data[i * 512 : (i + 1) * 512]
                ecc_bytes += HammingECC.calculate_512(chunk)

            oob_area = b"\xFF" + ecc_bytes + b"\xFF" * (64 - 1 - len(ecc_bytes))
            full_packet = pure_data + oob_area

            if self.mock_mode:
                self.mock_device.storage[BBT_PAGE_ID] = full_packet
                return True

            if self.protocol is None:
                return False
            self.protocol.erase_block(BBT_PHYSICAL_BLOCK)
            self.protocol.write_page(BBT_PAGE_ID, full_packet)
            if self.protocol.read_page(BBT_PAGE_ID) != full_packet:
                raise IOError("BBT read-back verification failed")
            return True
        except Exception as exc:
            self.log(f"Lỗi lưu BBT cố định: {exc}")
            return False

    def update_chip_info_ui(self):
        self.lbl_good_blocks.configure(text=f"• Khối Logic khả dụng (BBMS): {len(self.bbt_map)} Blocks")
        self.lbl_bad_blocks.configure(text=f"• Số khối hỏng (Bad Blocks): {len(self.bad_blocks_list)} Blocks")
        self.lbl_mode.configure(text=f"• Chế độ: {'Mock' if self.mock_mode else 'Board'}")

    def start_dump_thread(self):
        if self.ser is None and not self.mock_mode:
            self.log("Lỗi: Chưa kết nối cổng COM mạch RP2040!")
            return
        if not self.bbt_map:
            self.log("Không thể dump: chưa có bản đồ BBT hợp lệ.")
            return

        file_path = filedialog.asksaveasfilename(defaultextension=".bin", filetypes=[("Binary Files", "*.bin")])
        if file_path:
            threading.Thread(target=self.dump_process, args=(file_path,), daemon=True).start()

    def dump_process(self, file_path):
        self.toggle_buttons(False)
        self.log(f"Bắt đầu quá trình Dump dữ liệu sạch ra file: {os.path.basename(file_path)}")

        logical_blocks = list(self.bbt_map.values())
        max_logical_blocks = max(len(logical_blocks) - 1, 0)
        total_pages = max_logical_blocks * PAGES_PER_BLOCK
        current_page_count = 0

        try:
            with open(file_path, "wb") as f:
                for l_idx in range(max_logical_blocks):
                    p_block = self.bbt_map.get(l_idx)
                    if p_block is None:
                        continue
                    for page_offset in range(PAGES_PER_BLOCK):
                        actual_page_id = (p_block * PAGES_PER_BLOCK) + page_offset
                        raw_data = self.read_page(actual_page_id)
                        if not self.valid_page_data(raw_data):
                            self.log(f"Page {actual_page_id} không hợp lệ, bỏ qua.")
                            continue
                        pure_data = raw_data[0:2048]
                        oob_area = raw_data[2048:2112]
                        saved_ecc = oob_area[1:13]
                        fixed_data = bytearray()

                        for i in range(4):
                            chunk = pure_data[i * 512 : (i + 1) * 512]
                            status, fixed_chunk = HammingECC.correct_512(chunk, saved_ecc[i * 3 : (i + 1) * 3], HammingECC.calculate_512(chunk))
                            if status == -1:
                                self.log(f"[ECC Warn] Không sửa được lỗi tại block {p_block}, page {page_offset}")
                            fixed_data.extend(fixed_chunk)

                        f.write(fixed_data)
                        current_page_count += 1

                        if current_page_count % 16 == 0:
                            progress = (current_page_count / total_pages * 100) if total_pages else 100.0
                            self.safe_ui(self.progress_var.set, progress)
                            self.safe_ui(self.lbl_progress_desc.configure, text=f"Đang sao lưu: {current_page_count}/{total_pages} Pages ({int(progress)}%)")

            self.log("✓ QUÁ TRÌNH DUMP FILE HOÀN TẤT AN TOÀN!")
            self.safe_ui(self.lbl_progress_desc.configure, text="Dump chip hoàn tất dữ liệu sạch!")
        except Exception as exc:
            self.log(f"Lỗi trong quá trình Dump: {exc}")
        finally:
            self.safe_ui(self.progress_var.set, 0)
            self.toggle_buttons(True)

    def start_flash_thread(self):
        if self.ser is None and not self.mock_mode:
            self.log("Lỗi: Chưa kết nối cổng COM mạch RP2040!")
            return
        if not self.bbt_map:
            self.log("Không thể flash: chưa có bản đồ BBT hợp lệ.")
            return

        file_path = filedialog.askopenfilename(filetypes=[("Binary Files", "*.bin")])
        if file_path:
            confirm = messagebox.askyesno(
                "XÁC NHẬN GHI CHIP",
                "Thao tác này sẽ xóa sạch dữ liệu cũ và ghi đè file mới vào chip nhớ.\nBạn có chắc chắn không?",
            )
            if confirm:
                threading.Thread(target=self.flash_process, args=(file_path,), daemon=True).start()

    def load_bin_image(self, file_path):
        with open(file_path, "rb") as fh:
            data = fh.read()
        if data.startswith(BIN_MAGIC):
            if len(data) < 16:
                raise ValueError("Metadata của file .bin không hợp lệ")
            magic, version, page_size, oob_size, total_pages = struct.unpack("<8sIHHI", data[:20])
            if magic != BIN_MAGIC or version != BIN_VERSION:
                raise ValueError("File .bin không đúng định dạng NAND container")
            if page_size != 2048 or oob_size != 64:
                raise ValueError(f"File .bin có định dạng page_size={page_size}, oob_size={oob_size}, không tương thích")
            payload = data[20:]
            if len(payload) != total_pages * page_size:
                raise ValueError(f"Số byte payload không khớp metadata: expected {total_pages * page_size}, got {len(payload)}")
            return payload
        return data

    def flash_process(self, file_path):
        self.toggle_buttons(False)
        self.log(f"Bắt đầu quy trình nạp file dữ liệu: {os.path.basename(file_path)}")

        try:
            payload = self.load_bin_image(file_path)
        except Exception as exc:
            self.log(f"Không thể đọc định dạng file .bin: {exc}")
            self.toggle_buttons(True)
            return

        max_logical_blocks = max(len(self.bbt_map) - 1, 0)
        total_pages_in_file = len(payload) // 2048
        expected_blocks = (total_pages_in_file + PAGES_PER_BLOCK - 1) // PAGES_PER_BLOCK
        blocks_to_write = min(expected_blocks, max_logical_blocks)
        current_page_count = 0
        total_target_pages = blocks_to_write * PAGES_PER_BLOCK

        try:
            for l_block in range(blocks_to_write):
                p_block = self.bbt_map.get(l_block)
                if p_block is None:
                    continue
                self.erase_block(p_block)
                for page_offset in range(PAGES_PER_BLOCK):
                    offset = (l_block * PAGES_PER_BLOCK + page_offset) * 2048
                    if offset >= len(payload):
                        break
                    pure_data_2048 = payload[offset:offset + 2048]
                    if len(pure_data_2048) < 2048:
                        break
                    ecc_bytes = b""
                    for i in range(4):
                        chunk = pure_data_2048[i * 512 : (i + 1) * 512]
                        ecc_bytes += HammingECC.calculate_512(chunk)
                    oob_area = b"\xFF" + ecc_bytes + b"\xFF" * (64 - 1 - len(ecc_bytes))
                    full_packet = pure_data_2048 + oob_area
                    actual_page_id = (p_block * PAGES_PER_BLOCK) + page_offset
                    self.write_page(actual_page_id, full_packet)
                    current_page_count += 1

                    if current_page_count % 8 == 0:
                        progress = (current_page_count / total_target_pages * 100) if total_target_pages else 100.0
                        self.safe_ui(self.progress_var.set, progress)
                        self.safe_ui(self.lbl_progress_desc.configure, text=f"Đang ghi chip: {current_page_count}/{total_target_pages} Pages ({int(progress)}%)")

            self.log("✓ QUÁ TRÌNH FLASH NẠP CHIP HOÀN TẤT THÀNH CÔNG!")
            self.safe_ui(self.lbl_progress_desc.configure, text="Flash chip nhớ hoàn tất!")
        except Exception as exc:
            self.log(f"Lỗi trong quá trình Flash: {exc}")
        finally:
            self.safe_ui(self.progress_var.set, 0)
            self.toggle_buttons(True)


if __name__ == "__main__":
    app = NandFlashGuiApp()
    app.mainloop()
