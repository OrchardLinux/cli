"""Static conformance check: the registered module must satisfy Adapter."""
from orchard_cli.contracts import Adapter
from orchard_cli.j71b import adapter

J71B: Adapter = adapter
