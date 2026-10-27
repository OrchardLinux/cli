"""Static conformance check: the registered modules must satisfy Adapter."""
from orchard_cli.contracts import Adapter
from orchard_cli.j71b import adapter
from orchard_cli.j71t import adapter as j71t_adapter

J71B: Adapter = adapter
J71T: Adapter = j71t_adapter
