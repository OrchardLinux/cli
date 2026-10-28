"""Static conformance check: the registered module must satisfy Adapter."""
from orchard_cli.contracts import Adapter
from orchard_cli.j71b import adapter
from orchard_cli.p105 import adapter as p105

J71B: Adapter = adapter
P105: Adapter = p105
