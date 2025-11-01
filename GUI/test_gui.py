"""
Main GUI Window for Cryostat Control System (MOCK VERSION)

This runnable mock demonstrates the GUI's appearance and features 
without any backend hardware dependencies.
"""

import sys
import logging
import random
import time
from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QTimer

log = logging.getLogger(__name__)

# Global reference to main window instance for procedure access
main_window_instance = None


# --- MOCK CLASSES ---
# To replace all backend dependencies

class MockDevice:
    """Mock replacement for the backend Device class."""
    def __init__(self, device_type, address):
        self.device_type = device_type
        self.address = address
        self.connected = False
        self.in_test_gui = False
        log.info(f"MockDevice created: {device_type} at {address}")

    def connect(self):
        if self.address.upper() == 'FAIL': # Add a way to test failure
            raise ConnectionError("Mock Connection Error: Address 'FAIL' used.")
        self.connected = True
        log.info(f"MockDevice connected: {self.device_type}")

    def disconnect(self):
        self.connected = False
        log.info(f"MockDevice disconnected: {self.device_type}")

class MockDeviceManager:
    """Mock replacement for the backend DeviceManager."""
    def __init__(self):
        self.devices = {}
        log.info("MockDeviceManager created.")
        self.start_time = time.time()

    def get_state(self):
        """Return mock state data that changes over time."""
        elapsed = time.time() - self.start_time
        
        # Simulate some dynamic data
        vti_temp = 1.5 + abs(1.0 * (elapsed % 20 - 10)) + random.random() * 0.1
        sample_temp = 1.6 + abs(1.0 * (elapsed % 20 - 10)) + random.random() * 0.1
        field = 5.0 * (elapsed % 60 - 30) / 30 + random.random() * 0.01
        he_level = 75 - (elapsed / 60) % 10 + random.random() * 0.5
        
        return {
            'itc503': {
                'temperature_1': vti_temp,
                'temperature_2': sample_temp,
                'temperature_3': 4.2 + random.random() * 0.2,
                'temperature_setpoint': 1.5,
                'heater': 12.5 + random.random() * 2,
            },
            'mercuryips': {
                'field': field,
                'field_setpoint': 0.0,
                'switch_heater': 'OFF' if abs(field) < 0.1 else 'ON',
                'current': 45.0 + random.random() * 0.1,
            },
            'ilm210': {
                'helium_level': he_level,
                'nitrogen_level': 88.0 + random.random() * 0.5,
            }
        }

    def is_connected(self, device_type):
        """Mock check for quick controls. Let's just say 'yes'."""
        return True 

    def get_instrument(self, device_type):
        """Return a simple mock object so backend calls don't fail."""
        class MockInstrument:
            def __getattr__(self, name):
                log.warning(f"MockInstrument: Attribute '{name}' accessed.")
                # Return a dummy function for method calls
                return lambda *args, **kwargs: log.info(f"MockInstrument: Method '{name}' called with args={args}, kwargs={kwargs}")
        
        log.info(f"MockDeviceManager: get_instrument('{device_type}')")
        return MockInstrument()

    def shutdown_all(self):
        log.info("MockDeviceManager: shutdown_all() called.")

    def disconnect_all(self):
        log.info("MockDeviceManager: disconnect_all() called.")


class MockDeviceGUI(QtWidgets.QDialog):
    """Mock replacement for specific instrument GUIs (e.g., ITC503GUI)."""
    def __init__(self, device_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Mock GUI: {device_name}")
        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().addWidget(QtWidgets.QLabel(f"This is a mock control window for:\n{device_name}"))
        self.resize(300, 200)

class MockMeasurementWindow(QtWidgets.QDialog):
    """Mock replacement for specific measurement windows (e.g., TemperatureRampWindow)."""
    def __init__(self, measurement_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Mock Measurement: {measurement_name}")
        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().addWidget(QtWidgets.QLabel(f"This is a mock measurement window for:\n{measurement_name}"))
        
        # Add mock controls to simulate a measurement
        self.progress_bar = QtWidgets.QProgressBar()
        self.start_btn = QtWidgets.QPushButton("Start Mock Measurement")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        
        self.layout().addWidget(self.progress_bar)
        self.layout().addWidget(self.start_btn)
        self.layout().addWidget(self.stop_btn)
        
        self.start_btn.clicked.connect(self.start_measurement)
        self.stop_btn.clicked.connect(self.stop_measurement)
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_progress)
        self.progress = 0
        self.resize(400, 250)
        
        # Get main window instance
        global main_window_instance
        self.main_window = main_window_instance
        self.measurement_name = measurement_name

    def start_measurement(self):
        self.progress = 0
        self.timer.start(100) # Update 10x/sec
        self.start_btn.setEnabled(False)
        if self.main_window:
            self.main_window.set_measurement_status(self.measurement_name, "Running", 0)

    def update_progress(self):
        self.progress += 1
        if self.progress > 100:
            self.stop_measurement()
            if self.main_window:
                 self.main_window.set_measurement_status(self.measurement_name, "Completed", 100)
            return
        
        self.progress_bar.setValue(self.progress)
        if self.main_window:
            self.main_window.set_measurement_status(self.measurement_name, "Running", self.progress)

    def stop_measurement(self):
        self.timer.stop()
        self.start_btn.setEnabled(True)
        if self.main_window and self.progress < 100:
            self.main_window.set_measurement_status(self.measurement_name, "Stopped", self.progress)

    def closeEvent(self, event):
        """Ensure timer stops on close."""
        self.stop_measurement()
        if self.main_window:
            self.main_window.clear_measurement_status()
        super().closeEvent(event)

# --- END MOCK CLASSES ---


class DeviceSlotWidget(QtWidgets.QGroupBox):
    """Widget representing a single device slot with connection controls"""

    def __init__(self, device_type, device_name, default_address, parent=None):
        super().__init__(device_name, parent)
        self.device_type = device_type  # e.g., 'itc503', 'ilm210', 'mercuryips'
        self.device_name = device_name  # Display name
        self.default_address = default_address
        self.device = None  # Will hold MockDevice instance
        self.test_gui_window = None  # Track open test GUI

        self.setup_ui()

    def setup_ui(self):
        """Create the device slot UI"""
        layout = QtWidgets.QVBoxLayout(self)

        # Address input with status indicator
        address_layout = QtWidgets.QHBoxLayout()
        self.address_input = QtWidgets.QLineEdit(self.default_address)
        self.address_input.setPlaceholderText("Enter VISA address or 'mock'")
        address_layout.addWidget(self.address_input)

        # Status indicator (✓ or ✗)
        self.status_indicator = QtWidgets.QLabel("✗")
        self.status_indicator.setStyleSheet("color: red; font-weight: bold; font-size: 16pt;")
        self.status_indicator.setFixedWidth(30)
        self.status_indicator.setAlignment(QtCore.Qt.AlignCenter)
        address_layout.addWidget(self.status_indicator)
        layout.addLayout(address_layout)

        # Connect/Disconnect buttons
        conn_layout = QtWidgets.QHBoxLayout()
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_device)
        conn_layout.addWidget(self.connect_btn)

        self.disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_device)
        self.disconnect_btn.setEnabled(False)
        conn_layout.addWidget(self.disconnect_btn)
        layout.addLayout(conn_layout)

    def connect_device(self):
        """Connect to the device"""
        try:
            address = self.address_input.text().strip()
            if not address:
                QtWidgets.QMessageBox.warning(self, "Invalid Address",
                                              "Please enter a VISA address or 'mock'")
                return

            # --- MOCK IMPLEMENTATION ---
            # Create MockDevice instance
            self.device = MockDevice(self.device_type, address)
            self.device.connect()
            # --- END MOCK ---

            # Update UI
            self.status_indicator.setText("✓")
            self.status_indicator.setStyleSheet("color: green; font-weight: bold; font-size: 16pt;")
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self.address_input.setEnabled(False)

            log.info(f"Connected to {self.device_name} at {address}")

        except Exception as e:
            log.error(f"Failed to connect to {self.device_name}: {e}")
            QtWidgets.QMessageBox.critical(self, "Connection Error",
                                           f"Failed to connect to {self.device_name}:\n{e}")

    def disconnect_device(self):
        """Disconnect from the device"""
        try:
            # Close test GUI if open
            if self.test_gui_window:
                self.test_gui_window.close()
                self.test_gui_window = None

            # Disconnect device
            if self.device:
                self.device.disconnect()
                self.device = None

            # Update UI
            self.status_indicator.setText("✗")
            self.status_indicator.setStyleSheet("color: red; font-weight: bold; font-size: 16pt;")
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            self.address_input.setEnabled(True)

            log.info(f"Disconnected from {self.device_name}")

        except Exception as e:
            log.error(f"Failed to disconnect from {self.device_name}: {e}")

    def open_test_gui(self):
        """Open the device test GUI"""
        try:
            # Check if already open
            if self.test_gui_window:
                self.test_gui_window.raise_()
                self.test_gui_window.activateWindow()
                return

            # --- MOCK IMPLEMENTATION ---
            # Import appropriate test GUI
            if self.device_type == 'itc503':
                self.test_gui_window = MockDeviceGUI(self.device_name, parent=self)
            elif self.device_type == 'ilm210':
                self.test_gui_window = MockDeviceGUI(self.device_name, parent=self)
            elif self.device_type == 'mercuryips':
                self.test_gui_window = MockDeviceGUI(self.device_name, parent=self)
            else:
                QtWidgets.QMessageBox.warning(self, "Not Available",
                                              f"Test GUI for {self.device_name} not yet implemented.")
                return
            # --- END MOCK ---

            # Mark device as in test GUI mode
            if self.device:
                self.device.in_test_gui = True

            # Show window
            self.test_gui_window.show()

            # Clear reference when closed
            self.test_gui_window.destroyed.connect(self._on_test_gui_closed)

            log.info(f"Opened test GUI for {self.device_name}")

        except ImportError as e:
            log.error(f"Failed to import test GUI for {self.device_name}: {e}")
            QtWidgets.QMessageBox.critical(self, "Import Error",
                                           f"Test GUI for {self.device_name} not found:\n{e}")
        except Exception as e:
            log.error(f"Failed to open test GUI for {self.device_name}: {e}")
            QtWidgets.QMessageBox.critical(self, "Error",
                                           f"Failed to open test GUI:\n{e}")

    def _on_test_gui_closed(self):
        """Handle test GUI window close"""
        if self.device:
            self.device.in_test_gui = False
        self.test_gui_window = None
        log.info(f"Closed test GUI for {self.device_name}")


class DeviceListWidget(QtWidgets.QWidget):
    """Widget showing predefined device slots with connection controls"""

    def __init__(self, device_manager, parent=None):
        super().__init__(parent)
        self.device_manager = device_manager
        self.device_slots = {}  # device_type -> DeviceSlotWidget
        self.setup_ui()

    def setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # Title
        title = QtWidgets.QLabel("Devices")
        title.setStyleSheet("font-weight: bold; font-size: 14pt;")
        layout.addWidget(title)

        # Scroll area for device slots
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)

        # Container for device slots
        slots_container = QtWidgets.QWidget()
        slots_layout = QtWidgets.QVBoxLayout(slots_container)

        # Create predefined device slots
        device_configs = [
            ('itc503', 'ITC503 Temperature', 'GPIB::24'),
            ('ilm210', 'ILM210 Level Meter', 'ASRL4::INSTR'),
            ('mercuryips', 'Mercury iPS Magnet', 'GPIB::25'),
        ]

        for device_type, device_name, default_address in device_configs:
            slot = DeviceSlotWidget(device_type, device_name, default_address, self)
            self.device_slots[device_type] = slot
            slots_layout.addWidget(slot)

        slots_layout.addStretch()
        scroll.setWidget(slots_container)
        layout.addWidget(scroll)

        # Add separator
        layout.addWidget(QtWidgets.QLabel(""))  # Spacer

        # System State Control Box
        state_group = QtWidgets.QGroupBox("System State Control")
        state_layout = QtWidgets.QVBoxLayout()

        # Filling Helium button
        fill_he_btn = QtWidgets.QPushButton("Filling Helium")
        fill_he_btn.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold; padding: 8px;")
        fill_he_btn.clicked.connect(lambda: self.parent().start_filling_helium())
        state_layout.addWidget(fill_he_btn)

        # Filling Nitrogen button
        fill_n2_btn = QtWidgets.QPushButton("Filling Nitrogen")
        fill_n2_btn.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold; padding: 8px;")
        fill_n2_btn.clicked.connect(lambda: self.parent().start_filling_nitrogen())
        state_layout.addWidget(fill_n2_btn)

        # Changing Sample button
        change_sample_btn = QtWidgets.QPushButton("Changing Sample")
        change_sample_btn.setStyleSheet("background-color: #FF9800; color: white; font-weight: bold; padding: 8px;")
        change_sample_btn.clicked.connect(lambda: self.parent().start_changing_sample())
        state_layout.addWidget(change_sample_btn)

        state_group.setLayout(state_layout)
        layout.addWidget(state_group)

        layout.addStretch()

    def get_device(self, device_type):
        """Get Device instance for a specific device type"""
        if device_type in self.device_slots:
            return self.device_slots[device_type].device
        return None

    def is_device_connected(self, device_type):
        """Check if a device is connected"""
        device = self.get_device(device_type)
        return device and device.connected

    def connect_all(self):
        """Connect to all devices - removed, use individual slots instead"""
        log.info("Connect All clicked (feature removed, use individual slots)")
        pass

    def disconnect_all(self):
        """Disconnect from all devices"""
        log.info("Disconnecting all devices...")
        for slot in self.device_slots.values():
            if slot.device and slot.device.connected:
                slot.disconnect_device()

    def refresh_devices(self):
        """Refresh device status - no longer needed with real-time status"""
        pass


class LiveMonitorWidget(QtWidgets.QWidget):
    """Widget showing live monitoring of all device parameters"""

    def __init__(self, device_manager, parent=None):
        super().__init__(parent)
        self.device_manager = device_manager
        self.setup_ui()

        # Setup update timer
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_values)
        self.update_timer.start(1000)  # Update every 1 second

    def setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # Title
        title = QtWidgets.QLabel("Live Monitoring")
        title.setStyleSheet("font-weight: bold; font-size: 14pt;")
        layout.addWidget(title)

        # Scroll area for parameters
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(400)  # Increased height by 100%

        # Widget to hold all parameter displays
        self.param_widget = QtWidgets.QWidget()
        self.param_layout = QtWidgets.QVBoxLayout(self.param_widget)

        # Dictionary to store label widgets
        self.param_labels = {}

        # Create parameter groups
        self._create_temperature_group()
        self._create_magnet_group()
        self._create_level_meter_group()
        self._create_pressure_group()

        self.param_layout.addStretch()
        scroll.setWidget(self.param_widget)
        layout.addWidget(scroll)

        # Current Measurement Status Bar
        self.measurement_status = self._create_measurement_status_bar()
        layout.addWidget(self.measurement_status)

        # Action Log Panel
        self.action_log = self._create_action_log_panel()
        layout.addWidget(self.action_log)

    def _create_parameter_group(self, title, parameters):
        """Create a group box with parameters

        Args:
            title: Group title
            parameters: List of (label, key) tuples
        """
        group = QtWidgets.QGroupBox(title)
        group_layout = QtWidgets.QVBoxLayout()

        for label_text, key in parameters:
            param_layout = QtWidgets.QHBoxLayout()

            label = QtWidgets.QLabel(label_text + ":")
            label.setMinimumWidth(150)
            param_layout.addWidget(label)

            value_label = QtWidgets.QLabel("---")
            value_label.setStyleSheet("font-weight: bold;")
            value_label.setMinimumWidth(100)
            param_layout.addWidget(value_label)

            param_layout.addStretch()

            self.param_labels[key] = value_label
            group_layout.addLayout(param_layout)

        group.setLayout(group_layout)
        self.param_layout.addWidget(group)

    def _create_temperature_group(self):
        """Create temperature monitoring group"""
        params = [
            ("VTI Temperature", "vti_temp"),
            ("Sample Temperature", "sample_temp"),
            ("Rod Temperature", "rod_temp"),
            ("Current Setpoint", "temp_setpoint"),
            ("Heater Power", "heater_power"),
            ("Rod Heater Power", "rod_heater_power"),
        ]
        self._create_parameter_group("Temperature", params)

    def _create_magnet_group(self):
        """Create magnet monitoring group"""
        params = [
            ("Magnetic Field", "field"),
            ("Field Setpoint", "field_setpoint"),
            ("Switch Heater", "switch_heater"),
            ("Magnet Current", "magnet_current"),
            ("Power Supply Current", "ps_current"),
        ]
        self._create_parameter_group("Magnet", params)

    def _create_level_meter_group(self):
        """Create level meter monitoring group"""
        params = [
            ("Helium Level", "he_level"),
            ("Nitrogen Level", "n2_level"),
        ]
        self._create_parameter_group("Levels", params)

    def _create_pressure_group(self):
        """Create pressure monitoring group"""
        params = [
            ("Pressure", "pressure"),
        ]
        self._create_parameter_group("Pressure", params)

    def _create_measurement_status_bar(self):
        """Create a status bar showing current running measurement"""
        group = QtWidgets.QGroupBox("Current Measurement")
        layout = QtWidgets.QVBoxLayout()

        # Measurement name
        name_layout = QtWidgets.QHBoxLayout()
        name_layout.addWidget(QtWidgets.QLabel("Name:"))
        self.measurement_name_label = QtWidgets.QLabel("No measurement running")
        self.measurement_name_label.setStyleSheet("font-weight: bold; color: #666;")
        name_layout.addWidget(self.measurement_name_label)
        name_layout.addStretch()
        layout.addLayout(name_layout)

        # Progress bar
        progress_layout = QtWidgets.QVBoxLayout()
        progress_layout.addWidget(QtWidgets.QLabel("Progress:"))
        self.measurement_progress = QtWidgets.QProgressBar()
        self.measurement_progress.setValue(0)
        self.measurement_progress.setTextVisible(True)
        progress_layout.addWidget(self.measurement_progress)
        layout.addLayout(progress_layout)

        # Status message
        status_layout = QtWidgets.QHBoxLayout()
        status_layout.addWidget(QtWidgets.QLabel("Status:"))
        self.measurement_status_label = QtWidgets.QLabel("Idle")
        self.measurement_status_label.setStyleSheet("color: #666;")
        status_layout.addWidget(self.measurement_status_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        # Time elapsed
        time_layout = QtWidgets.QHBoxLayout()
        time_layout.addWidget(QtWidgets.QLabel("Elapsed:"))
        self.measurement_time_label = QtWidgets.QLabel("00:00:00")
        time_layout.addWidget(self.measurement_time_label)
        time_layout.addStretch()
        layout.addLayout(time_layout)

        group.setLayout(layout)
        return group

    def _create_action_log_panel(self):
        """Create a panel showing recent actions"""
        group = QtWidgets.QGroupBox("Action Log")
        layout = QtWidgets.QVBoxLayout()

        # Action log text area
        self.action_log_text = QtWidgets.QTextEdit()
        self.action_log_text.setReadOnly(True)
        self.action_log_text.setMaximumHeight(150)
        self.action_log_text.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
        layout.addWidget(self.action_log_text)

        # Clear log button
        clear_btn = QtWidgets.QPushButton("Clear Log")
        clear_btn.clicked.connect(lambda: self.parent().clear_action_log())
        layout.addWidget(clear_btn)

        group.setLayout(layout)
        return group

    def update_values(self):
        """Update all monitored values from the MockDeviceManager"""
        try:
            # This call now goes to MockDeviceManager.get_state()
            state = self.device_manager.get_state()

            # Update temperature values
            if 'itc503' in state:
                itc_state = state['itc503']
                self._update_label('vti_temp', itc_state.get('temperature_1'), 'K')
                self._update_label('sample_temp', itc_state.get('temperature_2'), 'K')
                self._update_label('rod_temp', itc_state.get('temperature_3'), 'K')
                self._update_label('temp_setpoint', itc_state.get('temperature_setpoint'), 'K')
                self._update_label('heater_power', itc_state.get('heater'), '%')

            # Update magnet values
            if 'mercuryips' in state:
                magnet_state = state['mercuryips']
                self._update_label('field', magnet_state.get('field'), 'T')
                self._update_label('field_setpoint', magnet_state.get('field_setpoint'), 'T')
                self._update_label('switch_heater', magnet_state.get('switch_heater'), '')
                self._update_label('magnet_current', magnet_state.get('current'), 'A')

            # Update level meter values
            if 'ilm210' in state:
                level_state = state['ilm210']
                self._update_label('he_level', level_state.get('helium_level'), '%')
                self._update_label('n2_level', level_state.get('nitrogen_level'), '%')

        except Exception as e:
            log.error(f"Error updating monitor values: {e}")
            # In a real mock, we might want to stop the timer if it fails repeatedly
            # self.update_timer.stop()
            # self._update_label('vti_temp', "ERROR", "") # Show error in GUI

    def _update_label(self, key, value, unit):
        """Update a parameter label with value and unit"""
        if key in self.param_labels and value is not None:
            label = self.param_labels[key]

            # Format value
            if isinstance(value, (int, float)):
                text = f"{value:.2f} {unit}"
            else:
                text = f"{value} {unit}"

            label.setText(text.strip())


class CryostatMainWindow(QtWidgets.QMainWindow):
    """Main window for Cryostat Control System"""

    def __init__(self, device_manager):
        super().__init__()
        self.device_manager = device_manager  # This will be the MockDeviceManager
        self.device_windows = {}  # Store open device windows
        self.procedure_windows = {}  # Store open procedure windows
        self.current_measurement = None  # Track current running measurement
        self.measurement_start_time = None  # Track measurement start time

        self.setup_ui()
        self.setWindowTitle("Cryostat Control System v3.1 (MOCK)")
        self.resize(1400, 900)

    def setup_ui(self):
        """Setup the main UI"""
        # Central widget
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        # Main layout - horizontal split
        main_layout = QtWidgets.QHBoxLayout(central)

        # Left panel - Device list
        self.device_list_widget = DeviceListWidget(self.device_manager, self)
        self.device_list_widget.setMaximumWidth(250)
        main_layout.addWidget(self.device_list_widget)

        # Center/Right panel - Monitoring and controls
        right_panel = QtWidgets.QVBoxLayout()

        # Top section - Live monitoring and quick controls
        top_section = QtWidgets.QHBoxLayout()

        # Live monitoring
        self.monitor_widget = LiveMonitorWidget(self.device_manager, self)
        top_section.addWidget(self.monitor_widget)

        # Quick controls panel
        quick_controls = self.create_quick_controls()
        top_section.addWidget(quick_controls)

        right_panel.addLayout(top_section, stretch=2)

        # Bottom section - Device control buttons
        device_controls = self.create_device_controls()
        right_panel.addWidget(device_controls, stretch=1)

        main_layout.addLayout(right_panel, stretch=1)

        # Setup menu bar
        self.create_menu_bar()

        # Setup status bar
        self.statusBar().showMessage("Ready (Mock Mode)")

        # Add initial log message
        self.log_action("Cryostat Control System v3.1 initialized (MOCK)")
        self.log_action("Ready for operation")

    def create_quick_controls(self):
        """Create quick control panel"""
        group = QtWidgets.QGroupBox("Quick Controls")
        layout = QtWidgets.QVBoxLayout()

        # Temperature Controls Section
        temp_group = QtWidgets.QGroupBox("Temperature Control")
        temp_layout = QtWidgets.QVBoxLayout()

        # Sample Temperature
        sample_temp_layout = QtWidgets.QHBoxLayout()
        sample_temp_layout.addWidget(QtWidgets.QLabel("Sample Temp (K):"))
        self.sample_temp_input = QtWidgets.QDoubleSpinBox()
        self.sample_temp_input.setRange(0, 400)
        self.sample_temp_input.setValue(300)
        self.sample_temp_input.setDecimals(2)
        sample_temp_layout.addWidget(self.sample_temp_input)
        temp_layout.addLayout(sample_temp_layout)

        # VTI Temperature
        vti_temp_layout = QtWidgets.QHBoxLayout()
        vti_temp_layout.addWidget(QtWidgets.QLabel("VTI Temp (K):"))
        self.vti_temp_input = QtWidgets.QDoubleSpinBox()
        self.vti_temp_input.setRange(0, 400)
        self.vti_temp_input.setValue(300)
        self.vti_temp_input.setDecimals(2)
        vti_temp_layout.addWidget(self.vti_temp_input)
        temp_layout.addLayout(vti_temp_layout)

        # Sweep Rate
        sweep_rate_layout = QtWidgets.QHBoxLayout()
        sweep_rate_layout.addWidget(QtWidgets.QLabel("Sweep Rate (K/min):"))
        self.sweep_rate_input = QtWidgets.QDoubleSpinBox()
        self.sweep_rate_input.setRange(0.1, 10)
        self.sweep_rate_input.setValue(1.0)
        self.sweep_rate_input.setDecimals(2)
        sweep_rate_layout.addWidget(self.sweep_rate_input)
        temp_layout.addLayout(sweep_rate_layout)

        # Temperature Sweep Buttons
        temp_btn_layout = QtWidgets.QHBoxLayout()
        start_sweep_btn = QtWidgets.QPushButton("Start Sweep")
        start_sweep_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        start_sweep_btn.clicked.connect(self.start_temperature_sweep)
        temp_btn_layout.addWidget(start_sweep_btn)

        stop_sweep_btn = QtWidgets.QPushButton("Stop Sweep")
        stop_sweep_btn.setStyleSheet("background-color: #F44336; color: white; font-weight: bold;")
        stop_sweep_btn.clicked.connect(self.stop_temperature_sweep)
        temp_btn_layout.addWidget(stop_sweep_btn)
        temp_layout.addLayout(temp_btn_layout)

        temp_group.setLayout(temp_layout)
        layout.addWidget(temp_group)

        # Magnet Controls Section
        magnet_group = QtWidgets.QGroupBox("Magnet Control")
        magnet_layout = QtWidgets.QVBoxLayout()

        # Field Setpoint
        field_layout = QtWidgets.QHBoxLayout()
        field_layout.addWidget(QtWidgets.QLabel("Field (T):"))
        self.field_input = QtWidgets.QDoubleSpinBox()
        self.field_input.setRange(-9, 9)
        self.field_input.setValue(0)
        self.field_input.setDecimals(3)
        field_layout.addWidget(self.field_input)
        magnet_layout.addLayout(field_layout)

        # Set Field Button
        set_field_btn = QtWidgets.QPushButton("Set Field")
        set_field_btn.clicked.connect(self.set_magnetic_field)
        magnet_layout.addWidget(set_field_btn)

        # Switch Heater Controls
        switch_heater_layout = QtWidgets.QHBoxLayout()
        self.switch_on_btn = QtWidgets.QPushButton("Switch ON")
        self.switch_on_btn.setStyleSheet("background-color: green; color: white;")
        self.switch_on_btn.clicked.connect(self.set_switch_heater_on)
        switch_heater_layout.addWidget(self.switch_on_btn)

        self.switch_off_btn = QtWidgets.QPushButton("Switch OFF")
        self.switch_off_btn.setStyleSheet("background-color: orange; color: white;")
        self.switch_off_btn.clicked.connect(self.set_switch_heater_off)
        switch_heater_layout.addWidget(self.switch_off_btn)
        magnet_layout.addLayout(switch_heater_layout)

        magnet_group.setLayout(magnet_layout)
        layout.addWidget(magnet_group)

        # Initiate Controls Section
        initiate_group = QtWidgets.QGroupBox("Initiate Controls")
        initiate_layout = QtWidgets.QVBoxLayout()

        # Initiate Magnet button
        init_magnet_btn = QtWidgets.QPushButton("Initiate Magnet")
        init_magnet_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        init_magnet_btn.clicked.connect(self.initiate_magnet)
        initiate_layout.addWidget(init_magnet_btn)

        # Initiate VTI button
        init_vti_btn = QtWidgets.QPushButton("Initiate VTI")
        init_vti_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        init_vti_btn.clicked.connect(self.initiate_vti)
        initiate_layout.addWidget(init_vti_btn)

        # Initiate Sample Heater button
        init_sample_btn = QtWidgets.QPushButton("Initiate Sample Heater")
        init_sample_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        init_sample_btn.clicked.connect(self.initiate_sample_heater)
        initiate_layout.addWidget(init_sample_btn)

        initiate_group.setLayout(initiate_layout)
        layout.addWidget(initiate_group)

        # Shutdown Controls Section
        shutdown_group = QtWidgets.QGroupBox("Shutdown Controls")
        shutdown_layout = QtWidgets.QVBoxLayout()

        # Magnet shutdown button
        magnet_shutdown_btn = QtWidgets.QPushButton("Magnet Shutdown")
        magnet_shutdown_btn.setStyleSheet("background-color: #FF6B6B; color: white; font-weight: bold;")
        magnet_shutdown_btn.clicked.connect(self.shutdown_magnet)
        shutdown_layout.addWidget(magnet_shutdown_btn)

        # VTI shutdown button
        vti_shutdown_btn = QtWidgets.QPushButton("VTI Shutdown")
        vti_shutdown_btn.setStyleSheet("background-color: #FF6B6B; color: white; font-weight: bold;")
        vti_shutdown_btn.clicked.connect(self.shutdown_vti)
        shutdown_layout.addWidget(vti_shutdown_btn)

        # Sample heater shutdown button
        sample_shutdown_btn = QtWidgets.QPushButton("Sample Heater Shutdown")
        sample_shutdown_btn.setStyleSheet("background-color: #FF6B6B; color: white; font-weight: bold;")
        sample_shutdown_btn.clicked.connect(self.shutdown_sample_heater)
        shutdown_layout.addWidget(sample_shutdown_btn)

        # Shutdown all button
        shutdown_all_btn = QtWidgets.QPushButton("Shutdown All Devices")
        shutdown_all_btn.setStyleSheet("background-color: red; color: white; font-weight: bold; padding: 5px;")
        shutdown_all_btn.clicked.connect(self.shutdown_all_devices)
        shutdown_layout.addWidget(shutdown_all_btn)

        shutdown_group.setLayout(shutdown_layout)
        layout.addWidget(shutdown_group)

        layout.addStretch()

        group.setLayout(layout)
        return group

    def create_device_controls(self):
        """Create device test GUI and measurement buttons"""
        group = QtWidgets.QGroupBox("Device Test GUIs & Measurements")
        layout = QtWidgets.QGridLayout()

        # Define device test GUI buttons
        test_gui_label = QtWidgets.QLabel("Device Test GUIs:")
        test_gui_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(test_gui_label, 0, 0, 1, 3)

        devices = [
            ("ITC503 Temperature", "itc503"),
            ("Mercury iPS Magnet", "mercuryips"),
            ("ILM210 Level Meter", "ilm210"),
        ]

        row = 1
        col = 0
        for label, device_name in devices:
            btn = QtWidgets.QPushButton(label)
            btn.clicked.connect(lambda checked, d=device_name: self.open_device_test_gui(d))
            layout.addWidget(btn, row, col)

            col += 1
            if col > 2:
                col = 0
                row += 1

        # Measurement buttons
        layout.addWidget(QtWidgets.QLabel(""), row + 1, 0)  # Spacer

        measurement_label = QtWidgets.QLabel("Measurements:")
        measurement_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(measurement_label, row + 2, 0, 1, 3)

        measurements = [
            ("Temperature Ramp", "temp_ramp"),
            ("Field Sweep", "field_sweep"),
            ("IV Sweep", "iv_sweep"),
        ]

        row = row + 3
        col = 0
        for label, meas_name in measurements:
            btn = QtWidgets.QPushButton(label)
            btn.clicked.connect(lambda checked, m=meas_name: self.open_measurement_window(m))
            layout.addWidget(btn, row, col)

            col += 1
            if col > 2:
                col = 0
                row += 1

        group.setLayout(layout)
        return group

    def create_menu_bar(self):
        """Create menu bar"""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu('&File')

        connect_action = QtWidgets.QAction('&Connect All Devices', self)
        connect_action.triggered.connect(self.device_list_widget.connect_all)
        file_menu.addAction(connect_action)

        disconnect_action = QtWidgets.QAction('&Disconnect All Devices', self)
        disconnect_action.triggered.connect(self.device_list_widget.disconnect_all)
        file_menu.addAction(disconnect_action)

        file_menu.addSeparator()

        exit_action = QtWidgets.QAction('E&xit', self)
        exit_action.setShortcut('Ctrl+Q')
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Devices menu
        devices_menu = menubar.addMenu('&Devices')

        itc_action = QtWidgets.QAction('&ITC503 Temperature', self)
        itc_action.triggered.connect(lambda: self.open_device_test_gui('itc503'))
        devices_menu.addAction(itc_action)

        magnet_action = QtWidgets.QAction('&Mercury iPS Magnet', self)
        magnet_action.triggered.connect(lambda: self.open_device_test_gui('mercuryips'))
        devices_menu.addAction(magnet_action)

        level_action = QtWidgets.QAction('&ILM210 Level Meter', self)
        level_action.triggered.connect(lambda: self.open_device_test_gui('ilm210'))
        devices_menu.addAction(level_action)

        # Measurements menu
        measurements_menu = menubar.addMenu('&Measurements')

        temp_ramp_action = QtWidgets.QAction('&Temperature Ramp', self)
        temp_ramp_action.triggered.connect(lambda: self.open_measurement_window('temp_ramp'))
        measurements_menu.addAction(temp_ramp_action)

        field_sweep_action = QtWidgets.QAction('&Field Sweep', self)
        field_sweep_action.triggered.connect(lambda: self.open_measurement_window('field_sweep'))
        measurements_menu.addAction(field_sweep_action)

        # Configuration menu (new)
        config_menu = menubar.addMenu('&Configuration')

        device_config_action = QtWidgets.QAction('&Device Settings', self)
        device_config_action.triggered.connect(self.open_device_config)
        config_menu.addAction(device_config_action)

        comm_config_action = QtWidgets.QAction('&Communication Settings', self)
        comm_config_action.triggered.connect(self.open_comm_config)
        config_menu.addAction(comm_config_action)

        config_menu.addSeparator()

        safety_config_action = QtWidgets.QAction('&Safety Limits', self)
        safety_config_action.triggered.connect(self.open_safety_config)
        config_menu.addAction(safety_config_action)

        alerts_config_action = QtWidgets.QAction('&Alert Settings', self)
        alerts_config_action.triggered.connect(self.open_alerts_config)
        config_menu.addAction(alerts_config_action)

        config_menu.addSeparator()

        save_config_action = QtWidgets.QAction('&Save Configuration', self)
        save_config_action.triggered.connect(self.save_configuration)
        config_menu.addAction(save_config_action)

        load_config_action = QtWidgets.QAction('&Load Configuration', self)
        load_config_action.triggered.connect(self.load_configuration)
        config_menu.addAction(load_config_action)

        # Logs menu
        logs_menu = menubar.addMenu('&Logs')

        cryostat_log_action = QtWidgets.QAction('&Cryostat Log', self)
        cryostat_log_action.triggered.connect(self.open_cryostat_log)
        logs_menu.addAction(cryostat_log_action)

        actions_log_action = QtWidgets.QAction('&Actions Log', self)
        actions_log_action.triggered.connect(self.open_actions_log)
        logs_menu.addAction(actions_log_action)

        monitoring_log_action = QtWidgets.QAction('&Monitoring Log', self)
        monitoring_log_action.triggered.connect(self.open_monitoring_log)
        logs_menu.addAction(monitoring_log_action)

        # Help menu
        help_menu = menubar.addMenu('&Help')

        about_action = QtWidgets.QAction('&About', self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def open_device_test_gui(self, device_type):
        """Open a device test GUI (shortcut to device slot's test GUI button)"""
        if device_type in self.device_list_widget.device_slots:
            slot = self.device_list_widget.device_slots[device_type]
            
            # --- MOCK BEHAVIOR ---
            # Check if device is "connected" in the mock GUI first
            if not slot.device or not slot.device.connected:
                QtWidgets.QMessageBox.warning(self, "Device Not Connected",
                                              f"{slot.device_name} is not connected.\n"
                                              "Please connect the device first.")
                return
            # --- END MOCK ---
                
            slot.open_test_gui()
        else:
            QtWidgets.QMessageBox.warning(self, "Device Not Found",
                                          f"Device slot for {device_type} not found.")

    def open_device_window(self, device_name):
        """Legacy method - redirects to test GUI"""
        self.open_device_test_gui(device_name)

    def open_measurement_window(self, measurement_name):
        """Open a measurement window (ManagedWindow)"""
        # Check if window already open and still valid
        if measurement_name in self.procedure_windows:
            window = self.procedure_windows[measurement_name]
            # Check if window still exists
            if window and not window.isHidden():
                window.raise_()
                window.activateWindow()
                return
            else:
                # Window was closed, remove from dict
                self.procedure_windows.pop(measurement_name, None)

        try:
            # --- MOCK IMPLEMENTATION ---
            # Import appropriate ManagedWindow
            if measurement_name == 'temp_ramp':
                window = MockMeasurementWindow("Temperature Ramp", parent=self)
            elif measurement_name == 'field_sweep':
                window = MockMeasurementWindow("Field Sweep", parent=self)
            elif measurement_name == 'iv_sweep':
                window = MockMeasurementWindow("IV Sweep", parent=self)
            else:
                QtWidgets.QMessageBox.warning(self, "Unknown Measurement",
                                              f"Measurement '{measurement_name}' not recognized.")
                return
            # --- END MOCK ---

            # Store and show window
            self.procedure_windows[measurement_name] = window
            window.show()

            # Remove from dict when closed using closeEvent
            def on_window_close():
                if measurement_name in self.procedure_windows:
                    self.procedure_windows.pop(measurement_name, None)
                    log.info(f"Closed measurement window: {measurement_name}")

            window.destroyed.connect(on_window_close)

            log.info(f"Opened measurement window: {measurement_name}")

        except ImportError as e:
            log.error(f"Failed to import measurement window {measurement_name}: {e}")
            QtWidgets.QMessageBox.critical(self, "Import Error",
                                           f"Failed to load measurement {measurement_name}:\n{e}")
        except Exception as e:
            log.error(f"Error opening measurement window {measurement_name}: {e}")
            QtWidgets.QMessageBox.critical(self, "Error",
                                           f"Error opening measurement window:\n{e}")

    def start_temperature_sweep(self):
        """MOCK: Start temperature sweep"""
        sample_temp = self.sample_temp_input.value()
        vti_temp = self.vti_temp_input.value()
        rate = self.sweep_rate_input.value()

        try:
            # --- MOCK IMPLEMENTATION ---
            # We skip device checks and just log the action
            action_msg = (f"MOCK: Starting temperature sweep: "
                          f"Sample={sample_temp}K, VTI={vti_temp}K, Rate={rate}K/min")
            log.info(action_msg)
            self.log_action(action_msg)
            self.statusBar().showMessage(f"Temperature sweep started to {vti_temp} K at {rate} K/min", 5000)
            # --- END MOCK ---

        except Exception as e:
            error_msg = f"Error starting temperature sweep: {e}"
            log.error(error_msg)
            self.log_action(f"ERROR: {error_msg}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to start sweep:\n{e}")

    def stop_temperature_sweep(self):
        """MOCK: Stop temperature sweep"""
        try:
            # --- MOCK IMPLEMENTATION ---
            action_msg = "MOCK: Stopping temperature sweep"
            log.info(action_msg)
            self.log_action(action_msg)
            self.statusBar().showMessage("Temperature sweep stopped", 3000)
            # --- END MOCK ---

        except Exception as e:
            error_msg = f"Error stopping temperature sweep: {e}"
            log.error(error_msg)
            self.log_action(f"ERROR: {error_msg}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to stop sweep:\n{e}")

    def set_magnetic_field(self):
        """MOCK: Set magnetic field"""
        field = self.field_input.value()

        try:
            # --- MOCK IMPLEMENTATION ---
            action_msg = f"MOCK: Setting magnetic field to {field} T"
            log.info(action_msg)
            self.log_action(action_msg)
            self.statusBar().showMessage(f"Field set to {field} T", 3000)
            # --- END MOCK ---

        except Exception as e:
            error_msg = f"Error setting magnetic field: {e}"
            log.error(error_msg)
            self.log_action(f"ERROR: {error_msg}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to set field:\n{e}")

    def set_switch_heater_on(self):
        """MOCK: Turn switch heater ON"""
        try:
            # --- MOCK IMPLEMENTATION ---
            action_msg = "MOCK: Turning switch heater ON"
            log.info(action_msg)
            self.log_action(action_msg)
            self.statusBar().showMessage("Switch heater ON", 3000)
            # --- END MOCK ---

        except Exception as e:
            error_msg = f"Error turning switch heater ON: {e}"
            log.error(error_msg)
            self.log_action(f"ERROR: {error_msg}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to turn switch heater ON:\n{e}")

    def set_switch_heater_off(self):
        """MOCK: Turn switch heater OFF"""
        reply = QtWidgets.QMessageBox.question(
            self, 'Switch Heater OFF',
            'Turn switch heater OFF? Make sure magnet is at target field!',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                # --- MOCK IMPLEMENTATION ---
                action_msg = "MOCK: Turning switch heater OFF"
                log.info(action_msg)
                self.log_action(action_msg)
                self.statusBar().showMessage("Switch heater OFF", 3000)
                # --- END MOCK ---

            except Exception as e:
                error_msg = f"Error turning switch heater OFF: {e}"
                log.error(error_msg)
                self.log_action(f"ERROR: {error_msg}")
                QtWidgets.QMessageBox.critical(self, "Error", f"Failed to turn switch heater OFF:\n{e}")

    def initiate_magnet(self):
        """MOCK: Initiate magnet system"""
        try:
            # --- MOCK IMPLEMENTATION ---
            action_msg = "MOCK: Initiating magnet system"
            log.info(action_msg)
            self.log_action(action_msg)
            self.statusBar().showMessage("Magnet system initiated", 3000)
            QtWidgets.QMessageBox.information(self, "Magnet Initiated",
                                              "MOCK: Magnet system has been initiated and is ready for operation.")
            # --- END MOCK ---

        except Exception as e:
            log.error(f"Error initiating magnet: {e}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to initiate magnet:\n{e}")

    def initiate_vti(self):
        """MOCK: Initiate VTI system"""
        try:
            # --- MOCK IMPLEMENTATION ---
            action_msg = "MOCK: Initiating VTI system"
            log.info(action_msg)
            self.log_action(action_msg)
            self.statusBar().showMessage("VTI system initiated", 3000)
            QtWidgets.QMessageBox.information(self, "VTI Initiated",
                                              "MOCK: VTI system has been initiated and is ready for operation.")
            # --- END MOCK ---

        except Exception as e:
            log.error(f"Error initiating VTI: {e}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to initiate VTI:\n{e}")

    def initiate_sample_heater(self):
        """MOCK: Initiate sample heater system"""
        try:
            # --- MOCK IMPLEMENTATION ---
            action_msg = "MOCK: Initiating sample heater system"
            log.info(action_msg)
            self.log_action(action_msg)
            self.statusBar().showMessage("Sample heater system initiated", 3000)
            QtWidgets.QMessageBox.information(self, "Sample Heater Initiated",
                                              "MOCK: Sample heater system has been initiated and is ready for operation.")
            # --- END MOCK ---

        except Exception as e:
            log.error(f"Error initiating sample heater: {e}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to initiate sample heater:\n{e}")

    def shutdown_magnet(self):
        """MOCK: Shutdown magnet safely"""
        reply = QtWidgets.QMessageBox.question(
            self, 'Magnet Shutdown',
            'Safely shutdown the magnet?\n\nThis will ramp field to zero.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                # --- MOCK IMPLEMENTATION ---
                action_msg = "MOCK: Shutting down magnet (ramping to zero)"
                log.info(action_msg)
                self.log_action(action_msg)
                self.statusBar().showMessage("Magnet shutdown initiated", 5000)
                # --- END MOCK ---

            except Exception as e:
                log.error(f"Error during magnet shutdown: {e}")
                QtWidgets.QMessageBox.critical(self, "Error", f"Magnet shutdown error:\n{e}")

    def shutdown_vti(self):
        """MOCK: Shutdown VTI heater"""
        reply = QtWidgets.QMessageBox.question(
            self, 'VTI Shutdown',
            'Shutdown VTI heater?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                # --- MOCK IMPLEMENTATION ---
                action_msg = "MOCK: Shutting down VTI heater"
                log.info(action_msg)
                self.log_action(action_msg)
                self.statusBar().showMessage("VTI heater shutdown", 3000)
                # --- END MOCK ---

            except Exception as e:
                log.error(f"Error during VTI shutdown: {e}")
                QtWidgets.QMessageBox.critical(self, "Error", f"VTI shutdown error:\n{e}")

    def shutdown_sample_heater(self):
        """MOCK: Shutdown sample heater"""
        reply = QtWidgets.QMessageBox.question(
            self, 'Sample Heater Shutdown',
            'Shutdown sample heater?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                # --- MOCK IMPLEMENTATION ---
                action_msg = "MOCK: Shutting down sample heater"
                log.info(action_msg)
                self.log_action(action_msg)
                self.statusBar().showMessage("Sample heater shutdown", 3000)
                # --- END MOCK ---

            except Exception as e:
                log.error(f"Error during sample heater shutdown: {e}")
                QtWidgets.QMessageBox.critical(self, "Error", f"Sample heater shutdown error:\n{e}")

    def shutdown_all_devices(self):
        """MOCK: Safely shutdown all devices"""
        reply = QtWidgets.QMessageBox.question(
            self, 'Shutdown All',
            'Safely shutdown all devices?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                # --- MOCK IMPLEMENTATION ---
                # This calls the mock method
                self.device_manager.shutdown_all()
                action_msg = "MOCK: Shutting down all devices"
                log.info(action_msg)
                self.log_action(action_msg)
                self.statusBar().showMessage("All devices shut down", 5000)
                # --- END MOCK ---
            except Exception as e:
                log.error(f"Error during shutdown: {e}")
                QtWidgets.QMessageBox.critical(self, "Shutdown Error",
                                               f"Error during shutdown:\n{e}")

    def show_about(self):
        """Show about dialog"""
        QtWidgets.QMessageBox.about(
            self, 'About Cryostat Control System',
            '<h2>Cryostat Control System v3.1</h2>'
            '<p>Built with PyMeasure</p>'
            '<p>Provides unified control and monitoring for cryostat devices</p>'
            '<b><p>(MOCK GUI VERSION)</p></b>'
        )

    def set_measurement_status(self, name, status="Running", progress=0):
        """
        Update the current measurement status display

        Args:
            name: Name of the measurement
            status: Status message (e.g., "Running", "Paused", "Completed")
            progress: Progress percentage (0-100)
        """
        # import time (already imported at top)

        self.current_measurement = name
        if self.measurement_start_time is None or status == "Running" and progress == 0:
            self.measurement_start_time = time.time()

        # Update the labels in the monitor widget
        self.monitor_widget.measurement_name_label.setText(name)
        self.monitor_widget.measurement_name_label.setStyleSheet("font-weight: bold; color: #2196F3;")
        self.monitor_widget.measurement_status_label.setText(status)

        # Set status color based on state
        if status == "Running":
            self.monitor_widget.measurement_status_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        elif status == "Paused" or status == "Stopped":
            self.monitor_widget.measurement_status_label.setStyleSheet("color: #FF9800; font-weight: bold;")
        elif status == "Completed":
            self.monitor_widget.measurement_status_label.setStyleSheet("color: #2196F3; font-weight: bold;")
            self.measurement_start_time = None # Stop timer
        elif status == "Error" or status == "Failed":
            self.monitor_widget.measurement_status_label.setStyleSheet("color: #F44336; font-weight: bold;")
            self.measurement_start_time = None # Stop timer
        
        # Update progress
        self.monitor_widget.measurement_progress.setValue(int(progress))

        # Update elapsed time
        if self.measurement_start_time:
            elapsed = time.time() - self.measurement_start_time
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            self.monitor_widget.measurement_time_label.setText(time_str)
        elif progress == 0:
             self.monitor_widget.measurement_time_label.setText("00:00:00")


    def clear_measurement_status(self):
        """Clear the measurement status (when no measurement is running)"""
        self.current_measurement = None
        self.measurement_start_time = None

        self.monitor_widget.measurement_name_label.setText("No measurement running")
        self.monitor_widget.measurement_name_label.setStyleSheet("font-weight: bold; color: #666;")
        self.monitor_widget.measurement_status_label.setText("Idle")
        self.monitor_widget.measurement_status_label.setStyleSheet("color: #666;")
        self.monitor_widget.measurement_progress.setValue(0)
        self.monitor_widget.measurement_time_label.setText("00:00:00")

    def log_action(self, message):
        """
        Log an action to the action log panel

        Args:
            message: Action message to log
        """
        # import time (already imported at top)
        timestamp = time.strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {message}"

        # Add to action log widget
        self.monitor_widget.action_log_text.append(log_entry)

        # Auto-scroll to bottom
        scrollbar = self.monitor_widget.action_log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear_action_log(self):
        """Clear the action log"""
        self.monitor_widget.action_log_text.clear()
        self.log_action("Action log cleared")

    def start_filling_helium(self):
        """Start helium filling procedure"""
        reply = QtWidgets.QMessageBox.warning(
            self, 'Filling Helium',
            'This will initiate the helium filling procedure.\n\n'
            'The magnet will be shut down safely first.\n\n'
            'Continue?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                log.info("Starting helium filling procedure")
                self.log_action("Starting helium filling procedure")

                # First, shutdown magnet (this calls the mock shutdown)
                self.statusBar().showMessage("Shutting down magnet for helium fill...", 3000)
                self.log_action("Shutting down magnet for helium fill...")
                self.shutdown_magnet() 
                # Note: shutdown_magnet() will show its own popup. This is preserved.

                # Set system state
                self.statusBar().showMessage("System ready for helium filling", 5000)
                self.log_action("System ready for helium filling")

                QtWidgets.QMessageBox.information(
                    self, 'Helium Filling',
                    'Magnet has been shut down.\n\n'
                    'System is ready for helium filling.\n\n'
                    'Please proceed with manual filling operations.'
                )

            except Exception as e:
                log.error(f"Error during helium filling preparation: {e}")
                QtWidgets.QMessageBox.critical(self, "Error",
                                               f"Error preparing for helium fill:\n{e}")

    def start_filling_nitrogen(self):
        """Start nitrogen filling procedure"""
        reply = QtWidgets.QMessageBox.warning(
            self, 'Filling Nitrogen',
            'This will initiate the nitrogen filling procedure.\n\n'
            'The magnet will be shut down safely first.\n\n'
            'Continue?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                log.info("Starting nitrogen filling procedure")
                self.log_action("Starting nitrogen filling procedure")

                # First, shutdown magnet (this calls the mock shutdown)
                self.statusBar().showMessage("Shutting down magnet for nitrogen fill...", 3000)
                self.log_action("Shutting down magnet for nitrogen fill...")
                self.shutdown_magnet()

                # Set system state
                self.statusBar().showMessage("System ready for nitrogen filling", 5000)
                self.log_action("System ready for nitrogen filling")

                QtWidgets.QMessageBox.information(
                    self, 'Nitrogen Filling',
                    'Magnet has been shut down.\n\n'
                    'System is ready for nitrogen filling.\n\n'
                    'Please proceed with manual filling operations.'
                )

            except Exception as e:
                log.error(f"Error during nitrogen filling preparation: {e}")
                QtWidgets.QMessageBox.critical(self, "Error",
                                               f"Error preparing for nitrogen fill:\n{e}")

    def start_changing_sample(self):
        """Start sample changing procedure"""
        reply = QtWidgets.QMessageBox.warning(
            self, 'Changing Sample',
            'This will prepare the system for sample change.\n\n'
            'The following will be shut down:\n'
            '- Magnet\n'
            '- VTI Heater\n'
            '- Sample Heater\n\n'
            'Continue?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            try:
                log.info("Starting sample change procedure")
                self.log_action("Starting sample change procedure")

                # Shutdown sequence (calls mock methods)
                self.statusBar().showMessage("Shutting down magnet...", 2000)
                self.log_action("Shutting down magnet for sample change...")
                self.shutdown_magnet()

                self.statusBar().showMessage("Shutting down VTI heater...", 2000)
                self.log_action("Shutting down VTI heater for sample change...")
                self.shutdown_vti()

                self.statusBar().showMessage("Shutting down sample heater...", 2000)
                self.log_action("Shutting down sample heater for sample change...")
                self.shutdown_sample_heater()

                self.statusBar().showMessage("System ready for sample change", 5000)
                self.log_action("System ready for sample change")

                QtWidgets.QMessageBox.information(
                    self, 'Sample Change Ready',
                    'All systems have been shut down safely.\n\n'
                    'System is ready for sample change.\n\n'
                    'Please wait for temperatures to stabilize before opening.'
                )

            except Exception as e:
                log.error(f"Error during sample change preparation: {e}")
                QtWidgets.QMessageBox.critical(self, "Error",
                                               f"Error preparing for sample change:\n{e}")

    def open_cryostat_log(self):
        """MOCK: Open cryostat log window"""
        QtWidgets.QMessageBox.information(
            self, 'Cryostat Log',
            'Cryostat log viewer will be implemented here.\n\n'
            'This will show system-level logs and events.'
        )
        log.info("Cryostat log requested (not yet implemented)")
        self.log_action("Cryostat log requested (not yet implemented)")


    def open_actions_log(self):
        """MOCK: Open actions log window"""
        QtWidgets.QMessageBox.information(
            self, 'Actions Log',
            'Actions log viewer will be implemented here.\n\n'
            'This will show all user actions and commands.'
        )
        log.info("Actions log requested (not yet implemented)")
        self.log_action("Actions log requested (not yet implemented)")

    def open_monitoring_log(self):
        """MOCK: Open monitoring log window"""
        QtWidgets.QMessageBox.information(
            self, 'Monitoring Log',
            'Monitoring log viewer will be implemented here.\n\n'
            'This will show continuous monitoring data and alerts.'
        )
        log.info("Monitoring log requested (not yet implemented)")
        self.log_action("Monitoring log requested (not yet implemented)")

    def open_device_config(self):
        """MOCK: Open device configuration window"""
        QtWidgets.QMessageBox.information(
            self, 'Device Configuration',
            'Device configuration window will be implemented here.\n\n'
            'This will allow configuration of device-specific settings.'
        )
        log.info("Device configuration requested (not yet implemented)")
        self.log_action("Device configuration requested (not yet implemented)")

    def open_comm_config(self):
        """MOCK: Open communication configuration window"""
        QtWidgets.QMessageBox.information(
            self, 'Communication Settings',
            'Communication settings window will be implemented here.\n\n'
            'This will allow configuration of GPIB/Serial communication parameters.'
        )
        log.info("Communication settings requested (not yet implemented)")
        self.log_action("Communication settings requested (not yet implemented)")

    def open_safety_config(self):
        """MOCK: Open safety limits configuration window"""
        QtWidgets.QMessageBox.information(
            self, 'Safety Limits Configuration',
            'Safety limits configuration window will be implemented here.\n\n'
            'This will allow setting of temperature, field, and other safety limits.'
        )
        log.info("Safety limits configuration requested (not yet implemented)")
        self.log_action("Safety limits configuration requested (not yet implemented)")

    def open_alerts_config(self):
        """MOCK: Open alerts configuration window"""
        QtWidgets.QMessageBox.information(
            self, 'Alert Settings',
            'Alert settings window will be implemented here.\n\n'
            'This will allow configuration of system alerts and notifications.'
        )
        log.info("Alert settings requested (not yet implemented)")
        self.log_action("Alert settings requested (not yet implemented)")

    def save_configuration(self):
        """MOCK: Save current configuration"""
        file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Configuration",
            "",
            "Configuration Files (*.cfg);;All Files (*)"
        )
        if file_name:
            self.log_action(f"MOCK: Saving configuration to {file_name}")
            QtWidgets.QMessageBox.information(
                self, 'Configuration Saved',
                f'Configuration would be saved to:\n{file_name}\n\n'
                'This feature will be implemented in a future version.'
            )

    def load_configuration(self):
        """MOCK: Load configuration from file"""
        file_name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load Configuration",
            "",
            "Configuration Files (*.cfg);;All Files (*)"
        )
        if file_name:
            self.log_action(f"MOCK: Loading configuration from {file_name}")
            QtWidgets.QMessageBox.information(
                self, 'Configuration Loaded',
                f'Configuration would be loaded from:\n{file_name}\n\n'
                'This feature will be implemented in a future version.'
            )

    def closeEvent(self, event):
        """Handle window close event"""
        reply = QtWidgets.QMessageBox.question(
            self, 'Exit',
            'Are you sure you want to exit?\nAll devices will be disconnected.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )

        if reply == QtWidgets.QMessageBox.Yes:
            # Close all child windows
            for window in list(self.device_windows.values()):
                window.close()
            for window in list(self.procedure_windows.values()):
                window.close()

            # Disconnect all devices (uses the mock method)
            try:
                # This call now goes to the widget's method
                self.device_list_widget.disconnect_all()
                log.info("All devices disconnected on exit.")
            except Exception as e:
                log.error(f"Error disconnecting devices on exit: {e}")

            event.accept()
        else:
            event.ignore()


def main():
    """Main entry point"""
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Create application
    app = QtWidgets.QApplication(sys.argv)

    # --- MOCK IMPLEMENTATION ---
    # Create Mock device manager
    # (Mock classes are defined at the top of this file)
    device_manager = MockDeviceManager()
    # --- END MOCK ---

    # Create and show main window
    global main_window_instance
    window = CryostatMainWindow(device_manager)
    main_window_instance = window  # Store global reference for procedures
    window.show()

    # Run application
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()