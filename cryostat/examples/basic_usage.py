"""
Basic Usage Example
===================

Demonstrates the basic usage of the cryostat control system with mock devices.

This example shows:
- Loading configuration
- Accessing devices
- Reading measurements
- Setting values
- Using high-level actions

Run this example:
    python -m cryostat.examples.basic_usage
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cryostat.core.cryostat import Cryostat
from cryostat.core.logger import setup_cryostat_logging
import logging


def main():
    """Main example demonstration."""

    # Setup logging
    setup_cryostat_logging(level=logging.INFO, console=True)

    print("=" * 70)
    print("Cryostat Control System - Basic Usage Example")
    print("=" * 70)
    print()

    # ==================== Initialize Cryostat ====================

    print("1. Initializing cryostat from configuration...")
    config_path = Path(__file__).parent.parent / 'config' / 'cryostat_mock.yaml'
    cryo = Cryostat(str(config_path))
    print(f"   ✓ Cryostat initialized with {len(cryo)} devices")
    print()

    # ==================== List Devices ====================

    print("2. Available devices:")
    for device_name in cryo.list_devices():
        device = cryo[device_name]
        print(f"   - {device_name}: {device.driver.__class__.__name__}")
    print()

    # ==================== Temperature Controller ====================

    print("3. Temperature Controller Operations:")
    print()

    # Get temperature controller
    temp_ctrl = cryo["vti_temp_controller"]

    # Initialize
    print("   Initializing temperature controller...")
    temp_ctrl.initiate()

    # Read current temperature
    temp1 = temp_ctrl.temperature_1
    temp2 = temp_ctrl.temperature_2
    print(f"   Current temperatures: T1={temp1:.2f}K, T2={temp2:.2f}K")

    # Read setpoint
    setpoint = temp_ctrl.temperature_setpoint
    print(f"   Current setpoint: {setpoint:.2f}K")

    # Change setpoint (low-level)
    print("   Setting new setpoint to 10.0K...")
    temp_ctrl.temperature_setpoint = 10.0
    print(f"   ✓ Setpoint updated")

    # High-level ramp (action layer)
    print("   Ramping to 4.2K (using action layer)...")
    final_temp = temp_ctrl.ramp_to_temperature(4.2, rate=1.0)
    print(f"   ✓ Ramp complete: {final_temp:.2f}K")

    # Get full status
    status = temp_ctrl.get_status()
    print(f"   Status: {status}")
    print()

    # ==================== Magnet Operations ====================

    print("4. Magnet Operations:")
    print()

    # Get magnet
    magnet = cryo["magnet1"]

    # Initialize
    print("   Initializing magnet...")
    magnet.initiate()

    # Read current field
    field = magnet.field
    print(f"   Current field: {field:.3f}T")

    # Check sweep status
    sweep_status = magnet.sweep_status
    print(f"   Sweep status: {sweep_status}")

    # Ramp to 0.5T (high-level action)
    print("   Ramping to 0.5T...")
    final_field = magnet.ramp_to_field(0.5, rate=0.1)
    print(f"   ✓ Field set: {final_field:.3f}T")

    # Read again
    field = magnet.field
    demand = magnet.demand_field
    persistent = magnet.persistent_field
    print(f"   Field readings:")
    print(f"     - Current: {field:.3f}T")
    print(f"     - Demand: {demand:.3f}T")
    print(f"     - Persistent: {persistent:.3f}T")

    # Go to zero
    print("   Ramping to zero...")
    magnet.go_to_zero(rate=0.2)
    print(f"   ✓ At zero field: {magnet.field:.4f}T")
    print()

    # ==================== Level Meter ====================

    print("5. Level Meter Operations:")
    print()

    # Get level meter
    level_meter = cryo["helium_level_meter"]

    # Initialize
    print("   Initializing level meter...")
    level_meter.initiate()

    # Measure all levels
    levels = level_meter.measure_all_levels()
    print(f"   Levels: {levels}")

    # Check for low level warnings
    warnings = level_meter.check_low_level_warning(threshold=20.0)
    print(f"   Low level warnings: {warnings}")
    print()

    # ==================== System Status ====================

    print("6. System-Wide Status:")
    print()

    all_status = cryo.get_all_status()
    print(f"   Retrieved status for {len(all_status)} devices")

    # Show magnet status as example
    if 'magnet1' in all_status:
        print(f"   Magnet1 status: {all_status['magnet1']}")

    print()

    # ==================== Lock Statistics ====================

    print("7. Lock Statistics:")
    print()

    lock_stats = cryo.get_lock_stats()
    print(f"   Total lock acquisitions: {lock_stats['acquisition_count']}")
    print(f"   Timeouts: {lock_stats['timeout_count']}")
    print(f"   Current owner: {lock_stats['current_owner']}")
    print()

    # ==================== Finalize ====================

    print("=" * 70)
    print("Example completed successfully!")
    print("=" * 70)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n\nERROR: {e}")
        import traceback
        traceback.print_exc()
