"""External data providers — pure I/O behind a Protocol.

Each provider exposes a Protocol in its own file (`providers/bus.py`,
future `providers/bikes.py`, ...). Concrete implementations live alongside
(`providers/tdx_bus.py` etc.). Services consume the Protocol so the
upstream system can be swapped or stubbed without reaching into private
module state.
"""
