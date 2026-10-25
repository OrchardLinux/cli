"""J71b facts from the independently installed hardware data package."""
from orchard_hardware import device, firmware_path

DEVICE = device('j71b')
PROFILE = DEVICE['device']['profile']
NAME = DEVICE['device']['name']
PRODUCT = DEVICE['match']['linux']['product']
CHIP = DEVICE['match']['dfu']['chip']
BOARD = DEVICE['match']['dfu']['board']
APPLE_PRODUCT = DEVICE['device']['apple_products'][0]
MANIFEST = firmware_path(PROFILE)
