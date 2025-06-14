import sys
import psutil
import time # Sử dụng time.time() để lấy timestamp dạng float đơn giản
from datetime import datetime # Vẫn cần để lấy timestamp ban đầu từ worker
from collections import deque # Sử dụng deque để giới hạn dữ liệu đồ thị

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTabWidget, QMenuBar, QInputDialog, QMessageBox, QSpinBox, QDialog,
    QDialogButtonBox, QFormLayout, QPushButton
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QCloseEvent

# pyqtgraph cần được cài đặt: pip install pyqtgraph psutil PyQt6
import pyqtgraph as pg
import random


# --- Cấu hình cho pyqtgraph ---
pg.setConfigOption('background', 'w') # Nền trắng
pg.setConfigOption('foreground', 'k') # Chữ đen
# -----------------------------

# --- Hằng số ---
INITIAL_UPDATE_INTERVAL_MS = 3000  # 3 giây
PLOT_LINE_WIDTH = 2 # *** Độ dày của đường đồ thị ***
# ---------------

class ProcessMonitorWorker(QObject):
    """
    Worker chạy trong thread riêng (mặc dù ở đây dùng QTimer nên ko cần thread riêng)
    để lấy dữ liệu process mà không chặn GUI.
    Gửi tín hiệu khi có dữ liệu mới hoặc lỗi.
    """
    # Vẫn gửi timestamp tuyệt đối, việc tính toán thời gian trôi qua sẽ do Tab thực hiện
    data_updated = pyqtSignal(int, float, float, float) # pid, absolute_timestamp, cpu_percent, memory_mb
    process_terminated = pyqtSignal(int) # pid
    process_error = pyqtSignal(int, str) # pid, error_message

    def __init__(self, pid, process_obj, initial_interval_ms):
        super().__init__()
        self.pid = pid
        self.process = process_obj
        self._running = True
        self._interval_ms = initial_interval_ms

        # Gọi cpu_percent() một lần khởi tạo
        try:
            self.process.cpu_percent(interval=None)
            time.sleep(0.1) # Đợi một chút
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self.process_error.emit(self.pid, "Process không tồn tại hoặc không có quyền truy cập khi khởi tạo.")
            self._running = False
            # Không cần khởi tạo timer nếu có lỗi ngay
            self.timer = None
            return

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.fetch_data)
        self.timer.start(self._interval_ms)

    def set_interval(self, interval_ms):
        """Thay đổi tần suất cập nhật."""
        self._interval_ms = interval_ms
        # Chỉ đặt lại interval nếu timer tồn tại và đang chạy
        if self.timer and self.timer.isActive():
            self.timer.setInterval(self._interval_ms)

    def stop(self):
        """Dừng việc lấy dữ liệu."""
        self._running = False
        if self.timer: # Kiểm tra timer tồn tại trước khi dừng
            self.timer.stop()

    def fetch_data(self):
        """Lấy dữ liệu CPU và RAM."""
        if not self._running or not self.timer:  # Kiểm tra cả self._running và self.timer
            return

        try:
            # Kiểm tra trước khi lấy dữ liệu để tránh lỗi nếu process chết đúng lúc
            if not self.process.is_running():
                self.process_terminated.emit(self.pid)
                self.stop()
                return

            # Lấy % CPU và chuyển sang IRIX mode
            cpu_percent = self.process.cpu_percent(interval=None)
            num_cores = psutil.cpu_count(logical=True)
            cpu_percent_irix = cpu_percent / num_cores  # Normalize to IRIX mode

            memory_info = self.process.memory_info()
            memory_mb = (memory_info.rss - memory_info.shared) / (1000 * 1000)  # Chuyển byte sang MB
            absolute_timestamp = datetime.now().timestamp()  # Lấy timestamp tuyệt đối

            # Emit dữ liệu CPU đã được chuẩn hóa
            self.data_updated.emit(self.pid, absolute_timestamp, cpu_percent_irix, memory_mb)

        except psutil.NoSuchProcess:
            self.process_terminated.emit(self.pid)
            self.stop()
        except psutil.AccessDenied:
            self.process_error.emit(self.pid, f"Không có quyền truy cập process PID {self.pid}.")
            self.stop()
        except Exception as e:
            self.process_error.emit(self.pid, f"Lỗi không xác định khi lấy dữ liệu: {e}")


class ProcessTabWidget(QWidget):
    """Widget hiển thị thông tin và đồ thị cho một process."""
    def __init__(self, pid, process_name, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.process_name = process_name
        self.terminated = False
        self.start_time = time.time()  # Ghi lại thời điểm bắt đầu monitor cho tab này

        # Dữ liệu cho đồ thị (lưu toàn bộ lịch sử)
        self.time_data = []
        self.cpu_data = []
        self.ram_data = []
        self.display_duration = 1800  # Mặc định hiển thị 60 giây gần nhất

        # --- Giao diện ---
        layout = QVBoxLayout(self)

        # Khu vực hiển thị thông tin hiện tại
        info_layout = QHBoxLayout()
        self.cpu_label = QLabel("CPU (IRIX Mode): -- %")
        self.ram_label = QLabel("RAM: -- MB")
        self.avg_cpu_label = QLabel("Avg CPU: -- %")  # New label for average CPU
        self.monitor_duration_label = QLabel("Monitor Duration: 00:00:00")
        self.status_label = QLabel(f"Monitoring PID: {self.pid}")  # Hiển thị trạng thái
        info_layout.addWidget(self.cpu_label)
        info_layout.addWidget(self.avg_cpu_label)  # Add the average CPU label
        info_layout.addWidget(self.ram_label)
        info_layout.addWidget(self.monitor_duration_label)

        info_layout.addStretch()
        info_layout.addWidget(self.status_label)
        layout.addLayout(info_layout)

        # Spinbox để đặt thời gian hiển thị
        duration_layout = QHBoxLayout()
        duration_label = QLabel("Display Duration (s):")
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setMinimum(10)  # Tối thiểu 10 giây
        self.duration_spinbox.setMaximum(3600000)  # Tối đa 1000 giờ
        self.duration_spinbox.setValue(self.display_duration)
        duration_layout.addWidget(duration_label)
        duration_layout.addWidget(self.duration_spinbox)

        # Add "Apply" button
        apply_button = QPushButton("Apply")
        apply_button.setStyleSheet("padding: 4px; font-size: 12px;")
        apply_button.clicked.connect(self.apply_display_duration)  # Connect to new method
        duration_layout.addWidget(apply_button)

        duration_layout.addStretch()
        layout.addLayout(duration_layout)

        # Đồ thị CPU
        self.cpu_plot_widget = pg.PlotWidget(title=f"CPU Usage (%) - {process_name}")
        self.cpu_plot_widget.setLabel('left', 'CPU', units='%')
        self.cpu_plot_widget.setLabel('bottom', 'Time Elapsed', units='s')
        self.cpu_plot_widget.showGrid(x=True, y=True)
        self.cpu_curve = self.cpu_plot_widget.plot(pen={'color': 'b', 'width': PLOT_LINE_WIDTH})

        # Enable downsampling and clipping for CPU plot
        self.cpu_curve.setDownsampling(auto=True, method='mean')
        self.cpu_curve.setClipToView(True)

        layout.addWidget(self.cpu_plot_widget)

        # Đồ thị RAM
        self.ram_plot_widget = pg.PlotWidget(title=f"RAM Usage (MB) - {process_name}")
        self.ram_plot_widget.setLabel('left', 'RAM', units='MB')
        self.ram_plot_widget.setLabel('bottom', 'Time Elapsed', units='s')
        self.ram_plot_widget.showGrid(x=True, y=True)
        self.ram_curve = self.ram_plot_widget.plot(pen={'color': 'r', 'width': PLOT_LINE_WIDTH})

        # Enable downsampling and clipping for RAM plot
        self.ram_curve.setDownsampling(auto=True, method='mean')
        self.ram_curve.setClipToView(True)

        layout.addWidget(self.ram_plot_widget)
        # ---------------

    def apply_display_duration(self):
        """Cập nhật thời gian hiển thị trên đồ thị khi nhấn nút Apply."""
        self.display_duration = self.duration_spinbox.value()
        self.update_plot()  # Cập nhật đồ thị ngay khi thay đổi thời gian hiển thị

    def update_display_duration(self, value):
        """Cập nhật thời gian hiển thị trên đồ thị."""
        self.display_duration = value
        self.update_plot()  # Cập nhật đồ thị ngay khi thay đổi thời gian hiển thị

    def update_data(self, absolute_timestamp, cpu_percent, memory_mb):
        """Cập nhật giao diện và dữ liệu đồ thị."""
        if self.terminated:
            return

        # Update the labels to reflect IRIX mode
        self.cpu_label.setText(f"CPU (IRIX Mode): <b>{cpu_percent:.2f} %</b>")
        self.ram_label.setText(f"RAM: <b>{memory_mb:.2f} MB</b>")

        # Lưu toàn bộ dữ liệu lịch sử
        elapsed = absolute_timestamp - self.start_time
        self.time_data.append(elapsed)
        self.cpu_data.append(cpu_percent)
        self.ram_data.append(memory_mb)

        # Calculate and update the average CPU usage
        avg_cpu = sum(self.cpu_data) / len(self.cpu_data)
        self.avg_cpu_label.setText(f"Avg CPU: <b>{avg_cpu:.2f} %</b>")

        # Update monitor duration label
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        seconds = int(elapsed % 60)
        self.monitor_duration_label.setText(f"Monitor Duration: <b>{hours:02d}:{minutes:02d}:{seconds:02d}</b>")

        # Cập nhật đồ thị
        self.update_plot()

    def update_plot(self):
        """Cập nhật đồ thị với dữ liệu trong khoảng thời gian hiển thị."""
        if not self.time_data:
            return

        # Lấy thời gian hiện tại và tính khoảng thời gian hiển thị
        current_time = self.time_data[-1]
        start_time = current_time - self.display_duration

        # Lọc dữ liệu trong khoảng thời gian hiển thị
        filtered_time = [t for t in self.time_data if t >= start_time]
        start_index = len(self.time_data) - len(filtered_time)
        filtered_cpu = self.cpu_data[start_index:]
        filtered_ram = self.ram_data[start_index:]

        # Cập nhật đồ thị với dữ liệu đã lọc
        self.cpu_curve.setData(filtered_time, filtered_cpu)
        self.ram_curve.setData(filtered_time, filtered_ram)

    def mark_terminated(self):
        """Đánh dấu process đã kết thúc và cập nhật giao diện."""
        self.terminated = True
        self.status_label.setText(f"PID: {self.pid} (Terminated)")
        self.status_label.setStyleSheet("color: red;")
        #self.cpu_label.setText("CPU: -- %")
        #self.ram_label.setText("RAM: -- MB")
        #self.avg_cpu_label.setText("Avg CPU: -- %")  # Cập nhật nhãn CPU trung bình

    def mark_error(self, error_message):
        """Hiển thị lỗi trên tab."""
        self.terminated = True
        self.status_label.setText(f"PID: {self.pid} (Error)")
        self.status_label.setStyleSheet("color: orange;")
        QMessageBox.warning(self, f"Process Error (PID: {self.pid})", error_message)


class IntervalDialog(QDialog):
    """Hộp thoại để người dùng nhập khoảng thời gian cập nhật."""
    def __init__(self, current_interval_sec, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Update Interval")

        layout = QFormLayout(self)

        self.interval_spinbox = QSpinBox()
        self.interval_spinbox.setMinimum(1) # Tối thiểu 1 giây
        self.interval_spinbox.setMaximum(300) # Tối đa 5 phút
        self.interval_spinbox.setValue(current_interval_sec)
        self.interval_spinbox.setSuffix(" s")

        layout.addRow("Update Interval:", self.interval_spinbox)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def get_interval_sec(self):
        """Trả về giá trị interval người dùng đã chọn (tính bằng giây)."""
        return self.interval_spinbox.value()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Process Monitor @v1.0-khuongnv2")
        self.setGeometry(100, 100, 900, 700)

        self.monitored_processes = {}
        self.update_interval_ms = INITIAL_UPDATE_INTERVAL_MS

        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self.close_tab)

        # Placeholder widget for when no processes are monitored
        self.placeholder_widget = QWidget()
        placeholder_layout = QVBoxLayout(self.placeholder_widget)
        placeholder_label = QLabel("No processes are being monitored.\n\n"
                                   "Use the 'Actions -> Add Process...' menu to start monitoring a process.\n\n"
                                      "For more information, check the 'Help -> User Guide' menu.")
        placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_label.setStyleSheet("font-size: 16px; color: gray;")
        placeholder_layout.addWidget(placeholder_label)

        self.setCentralWidget(self.tab_widget)
        self.tab_widget.addTab(self.placeholder_widget, "Welcome")
        self.tab_widget.tabBar().setVisible(False)  # Hide the tab bar when only the placeholder is shown

        # Menu bar
        menu_bar = self.menuBar()
        action_menu = menu_bar.addMenu("&Actions")
        settings_menu = menu_bar.addMenu("&Settings")
        help_menu = menu_bar.addMenu("&Help")  # Add Help menu

        # Actions menu
        add_action = QAction("&Add Process...", self)
        add_action.triggered.connect(self.add_process_dialog)
        action_menu.addAction(add_action)

        action_menu.addSeparator()

        exit_action = QAction("&Exit", self)
        exit_action.triggered.connect(self.close)
        action_menu.addAction(exit_action)

        # Settings menu
        interval_action = QAction("Set &Update Interval...", self)
        interval_action.triggered.connect(self.set_update_interval_dialog)
        settings_menu.addAction(interval_action)

        # Help menu
        guide_action = QAction("&User Guide", self)
        guide_action.triggered.connect(self.show_user_guide)
        help_menu.addAction(guide_action)

        self.show()

    def show_user_guide(self):
        """Hiển thị hướng dẫn sử dụng."""
        guide_text = (
            "<h2>Process Monitor User Guide</h2>"
            "<p><b>Welcome to Process Monitor!</b></p>"
            "<p>This application allows you to monitor the CPU and RAM usage of specific processes in real-time.</p>"
            "<h3>How to Use:</h3>"
            "<ul>"
            "  <li><b>Add a Process:</b> Go to <i>Actions -> Add Process...</i> and enter the name of the process you want to monitor.</li>"
            "  <li><b>Set Update Interval:</b> Go to <i>Settings -> Set Update Interval...</i> to adjust the frequency of updates (in seconds).</li>"
            "  <li><b>Close a Tab:</b> Click the close button on a tab to stop monitoring a process.</li>"
            "  <li><b>Exit:</b> Go to <i>Actions -> Exit</i> to close the application.</li>"
            "</ul>"
            "<h3>Features:</h3>"
            "<ul>"
            "  <li>Real-time CPU and RAM usage monitoring.</li>"
            "  <li>Adjustable display duration for plots.</li>"
            "  <li>Dynamic addition and removal of monitored processes.</li>"
            "  <li><b>Export Data:</b> You can export the plot data to an image or CSV file:</li>"
            "  <ul>"
            "    <li><b>Export to Image:</b> Right-click on the plot and select <i>Export</i> to save the plot as an image file (e.g., PNG).</li>"
            "    <li><b>Export to CSV:</b> Use the <i>Export to CSV</i> option in the context menu to save the data as a CSV file.</li>"
            "  </ul>"
            "</ul>"
            "<h3>Notes:</h3>"
            "<ul>"
            "  <li>Ensure you have the necessary permissions to monitor the selected processes.</li>"
            "  <li>Processes that terminate will automatically stop being monitored.</li>"
            "</ul>"
            "<p>For further assistance, contact support at <i>khuongnv2@viettel.com.vn</i>.</p>"
        )

        QMessageBox.information(self, "User Guide", guide_text)

    def find_process_by_name(self, target_name):
        """Tìm process dựa trên tên (không phân biệt hoa thường)."""
        target_name_lower = target_name.lower()
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if target_name_lower in proc.info['name'].lower() and proc.pid not in self.monitored_processes:
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return None

    def add_process_dialog(self):
        """Hiển thị hộp thoại yêu cầu người dùng nhập tên process."""
        process_name, ok = QInputDialog.getText(self, "Add Process", "Enter process name:")

        if ok and process_name:
            process = self.find_process_by_name(process_name)
            if process:
                self.add_process_tab(process)
            else:
                already_monitored_pid = None
                for pid, data in self.monitored_processes.items():
                     if 'tab' in data and process_name.lower() in data['tab'].process_name.lower():
                         already_monitored_pid = pid
                         break

                if already_monitored_pid is not None:
                     QMessageBox.information(self, "Process Already Monitored",
                                           f"Process '{self.monitored_processes[already_monitored_pid]['tab'].process_name}' (PID: {already_monitored_pid}) matching '{process_name}' is already being monitored.")
                     if 'tab_index' in self.monitored_processes[already_monitored_pid]:
                         tab_index = self.monitored_processes[already_monitored_pid]['tab_index']
                         if 0 <= tab_index < self.tab_widget.count():
                             self.tab_widget.setCurrentIndex(tab_index)
                else:
                    QMessageBox.warning(self, "Process Not Found",
                                        f"No running process found matching '{process_name}' that isn't already monitored.")
        elif ok and not process_name:
             QMessageBox.warning(self, "Input Error", "Process name cannot be empty.")


    def add_process_tab(self, process):
        """Tạo tab mới cho process được tìm thấy."""
        pid = process.pid
        process_name = process.name()

        if pid in self.monitored_processes:
            QMessageBox.information(self, "Already Monitoring", f"Process '{process_name}' (PID: {pid}) is already being monitored.")
            return

        tab_content = ProcessTabWidget(pid, process_name)
        worker = ProcessMonitorWorker(pid, process, self.update_interval_ms)

        # Kết nối tín hiệu, sử dụng lambda để khớp tham số slot
        worker.data_updated.connect(lambda p, ts, cpu, ram: tab_content.update_data(ts, cpu, ram))
        worker.process_terminated.connect(self.handle_process_terminated)
        worker.process_error.connect(self.handle_process_error)

        # Chỉ thêm tab và lưu thông tin nếu worker được khởi tạo thành công
        if worker.timer is not None:  # Kiểm tra xem timer có được tạo không (tức là ko lỗi ngay)
            if self.tab_widget.indexOf(self.placeholder_widget) != -1:
                self.tab_widget.removeTab(self.tab_widget.indexOf(self.placeholder_widget))
                self.tab_widget.tabBar().setVisible(True)  # Show the tab bar when a process is added

            tab_index = self.tab_widget.addTab(tab_content, f"{process_name} ({pid})")
            self.tab_widget.setCurrentIndex(tab_index)

            self.monitored_processes[pid] = {
                'worker': worker,
                'tab': tab_content,
                'tab_index': tab_index
            }
            self._update_tab_indices()
        else:
            # Nếu worker bị lỗi ngay khi tạo, không thêm tab và báo lỗi
            QMessageBox.critical(self, "Initialization Error", f"Could not start monitoring process '{process_name}' (PID: {pid}). It might have terminated or access was denied.")


    def close_tab(self, index):
        """Xử lý khi người dùng nhấn nút đóng tab."""
        widget_to_close = self.tab_widget.widget(index)
        if isinstance(widget_to_close, ProcessTabWidget):
            pid_to_remove = widget_to_close.pid
            if pid_to_remove in self.monitored_processes:
                # Lấy worker từ dict trước khi xóa entry
                worker_to_stop = self.monitored_processes[pid_to_remove].get('worker')
                if worker_to_stop:
                    worker_to_stop.stop()
                del self.monitored_processes[pid_to_remove]
                print(f"Stopped monitoring process PID: {pid_to_remove}")

            self.tab_widget.removeTab(index)
            self._update_tab_indices()  # Cập nhật index sau khi xóa

        # Show the placeholder widget if no tabs are left
        if self.tab_widget.count() == 0:
            self.tab_widget.addTab(self.placeholder_widget, "Welcome")
            self.tab_widget.tabBar().setVisible(False)  # Hide the tab bar when only the placeholder is shown

    def _update_tab_indices(self):
        """Cập nhật lại 'tab_index' trong self.monitored_processes."""
        pids_to_remove = []
        for pid, data in self.monitored_processes.items():
            widget = data.get('tab')
            if widget:
                try:
                    current_index = self.tab_widget.indexOf(widget)
                    if current_index != -1:
                        data['tab_index'] = current_index
                    else:
                        # Widget không còn trong tab_widget, đánh dấu để xóa
                        print(f"Warning: Tab for PID {pid} not found, marking for removal.")
                        pids_to_remove.append(pid)
                except Exception as e:
                     print(f"Error updating tab index for PID {pid}: {e}. Marking for removal.")
                     pids_to_remove.append(pid)
            else:
                # Không có widget, đánh dấu để xóa
                print(f"Warning: Inconsistent data for PID {pid}, marking for removal.")
                pids_to_remove.append(pid)

        # Xóa các entry không hợp lệ sau khi duyệt xong
        for pid in pids_to_remove:
            if pid in self.monitored_processes:
                 worker_to_stop = self.monitored_processes[pid].get('worker')
                 if worker_to_stop:
                     worker_to_stop.stop() # Đảm bảo worker dừng lại
                 del self.monitored_processes[pid]


    def handle_process_terminated(self, pid):
        """Xử lý khi nhận được tín hiệu process đã kết thúc."""
        print(f"Process PID {pid} terminated.")
        if pid in self.monitored_processes:
            # Kiểm tra xem 'tab' có tồn tại không trước khi truy cập
            tab = self.monitored_processes[pid].get('tab')
            if tab:
                tab.mark_terminated()
            # Worker đã tự dừng

    def handle_process_error(self, pid, error_message):
        """Xử lý khi nhận được tín hiệu lỗi từ worker."""
        print(f"Error monitoring process PID {pid}: {error_message}")
        if pid in self.monitored_processes:
             # Kiểm tra xem 'tab' có tồn tại không trước khi truy cập
            tab = self.monitored_processes[pid].get('tab')
            if tab:
                tab.mark_error(error_message)
            # Worker thường đã tự dừng khi phát hiện lỗi

    def set_update_interval_dialog(self):
        """Mở hộp thoại để đặt khoảng thời gian cập nhật."""
        current_interval_sec = self.update_interval_ms // 1000
        dialog = IntervalDialog(current_interval_sec, self)
        if dialog.exec():
            new_interval_sec = dialog.get_interval_sec()
            self.update_interval_ms = new_interval_sec * 1000
            print(f"Set update interval to {new_interval_sec} seconds.")
            for pid_data in self.monitored_processes.values():
                # Kiểm tra cả tab và worker trước khi cập nhật interval
                tab = pid_data.get('tab')
                worker = pid_data.get('worker')
                if tab and not tab.terminated and worker:
                    worker.set_interval(self.update_interval_ms)

    def closeEvent(self, event: QCloseEvent):
        """Được gọi khi cửa sổ chính sắp đóng."""
        reply = QMessageBox.question(self, 'Confirm Exit',
                                     'Are you sure you want to exit? Monitoring will stop.',
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            print("Stopping all monitors...")
            for pid_data in self.monitored_processes.values():
                 # Kiểm tra worker tồn tại trước khi dừng
                worker = pid_data.get('worker')
                if worker:
                    worker.stop()
            print("Monitors stopped. Exiting.")
            event.accept()
        else:
            event.ignore()

# --- Chạy ứng dụng ---
if __name__ == "__main__":
    # Bật antialiasing cho đồ thị mượt hơn (tùy chọn)
    pg.setConfigOptions(antialias=True)

    app = QApplication(sys.argv)
    main_window = MainWindow()
    sys.exit(app.exec())
# --- Kết thúc ---