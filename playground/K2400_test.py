from pymeasure.adapters import FakeAdapter
from pymeasure.instruments.keithley import Keithley2400

# 1. Create an instance of the FakeAdapter
adapter = FakeAdapter()

# 2. Pass the adapter object to the instrument
keithley = Keithley2400(adapter)

# 3. Now you can test your instrument communication
# The FakeAdapter will store commands in its buffer
keithley.source_voltage = 5.0

# You can then check what was "sent" to the instrument
print(f"Command sent to adapter: {adapter.read()}")

# You can also pre-load responses for measurement commands
adapter.write("1.234") # Pre-load a fake voltage reading
print(f"Measured voltage: {keithley.voltage}") # This will read "1.234"