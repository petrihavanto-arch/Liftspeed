from .base import DistanceSensor
from .dummy import DummySensor


def create_sensor(args) -> DistanceSensor:
    if args.platform == "pc":
        return DummySensor()
    elif args.platform == "rpi":
        from .vl53l1x_sensor import VL53L1XSensor
        return VL53L1XSensor(i2c_bus=args.i2c_bus, i2c_address=args.i2c_address)
    raise ValueError(f"unknown platform: {args.platform!r}")
