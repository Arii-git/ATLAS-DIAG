# Hardware

ATLAS-DIAG needs one NUCLEO-F446RE board, two breadboards, a 0.96-inch SSD1306 OLED, two LEDs, a push button and a handful of resistors. The chip under test is pressed straight into a small breadboard; there is no ZIF socket.

![Bench prototype](images/bench-overview.jpg)

*1 NUCLEO-F446RE, 2 main breadboard, 3 chip under test, 4 1 kΩ series resistors, 5 OLED, 6 PASS and FAULT LEDs.*

## Design choices

Every chip pin except 7 and 14 goes to an STM32 GPIO through its own 1 kΩ series resistor, twelve lines in all. The firmware switches each line between output (to drive a chip input) and input (to read a chip output) for each chip type, so the wiring stays the same when you swap chips. If a GPIO ever drives against a chip output, for example when the wrong chip is inserted or the Identify feature tries another pin-out, the resistor limits the current to about 5 mA.

Pin 7 goes to the GND rail and pin 14 to the 5 V rail by short direct wires with no resistor. The rails take their power from the NUCLEO's 5V and GND pins. A long or loose ground wire shows up as failures that get worse as more inputs are driven high. If you have a 100 nF capacitor, fit it between DIP 14 and DIP 7, across the supply. Never put it in series with pin 14, because that leaves the chip without a proper supply.

There is no level shifter. The STM32 drives 3.3 V, above the 2.0 V input-high threshold of 74HCT logic, and the chip's 5 V outputs go into pins that are 5 V tolerant. The firmware keeps idle and unused lines as digital inputs, never analog, because the 5 V tolerance does not apply in analog mode.

This only works for HCT parts. Plain 74HC chips powered at 5 V need roughly 3.5 V for a valid logic high, which a 3.3 V output does not reliably give; use the 74HCT family or add level shifting.

## Chip wiring (pin by pin)

This table matches the bench wiring and `pin_map[]` in `firmware/Core/Src/atlas_app.c`. "in" means a chip input (the board drives it), "out" means a chip output (the board reads it). The header column gives the NUCLEO-F446RE Morpho connector pin used on the bench.

| DIP pin | STM32 pin | Header | 74HCT00 / 08 / 32 / 86 | 74HCT02 | 74HCT04 |
|---|---|---|---|---|---|
| 1 | PC2 | CN7-35 | in (G1) | out (G1) | in (G1) |
| 2 | PC3 | CN7-37 | in (G1) | in (G1) | out (G1) |
| 3 | PC4 | CN10-34 | out (G1) | in (G1) | in (G2) |
| 4 | PC5 | CN10-6 | in (G2) | out (G2) | out (G2) |
| 5 | PC6 | CN10-4 | in (G2) | in (G2) | in (G3) |
| 6 | PC7 | CN10-19 | out (G2) | in (G2) | out (G3) |
| 7 | none | GND rail | GND | GND | GND |
| 8 | PC9 | CN10-1 | out (G3) | in (G3) | out (G4) |
| 9 | PC10 | CN7-1 | in (G3) | in (G3) | in (G4) |
| 10 | PC11 | CN7-2 | in (G3) | out (G3) | out (G5) |
| 11 | PC12 | CN7-3 | out (G4) | in (G4) | in (G5) |
| 12 | PD2 | CN7-4 | in (G4) | in (G4) | out (G6) |
| 13 | PB1 | CN10-24 | in (G4) | out (G4) | in (G6) |
| 14 | none | 5 V rail | +5 V | +5 V | +5 V |

`pin_map[]` also lists PC8 and PB2 at the positions of DIP 7 and DIP 14. They are not wired; every chip profile marks those two positions as unused (`X`), so the firmware never drives them.

Per-chip pin configurations as sent in the `CFG:` command (DIP pin 1 first, `I` = chip input, `O` = chip output, `X` = power):

| Chip | Configuration |
|---|---|
| 74HCT00, 74HCT08, 74HCT32, 74HCT86 | `IIOIIOXOIIOIIX` |
| 74HCT02 | `OIIOIIXIIOIIOX` |
| 74HCT04 | `IOIOIOXOIOIOIX` |

The chip sits across the centre gap of its breadboard with the notch facing left, so pin 1 is at the bottom left and lands on the PC2 line. Use the same orientation every time.

## Other connections

| Part | Connection | Notes |
|---|---|---|
| SSD1306 OLED, 128 x 64, I2C address 0x3C | SCL to PB8 (CN10-3, D15), SDA to PB9 (CN10-5, D14), VCC to the 5 V rail, GND to the GND rail | I2C1 at 100 kHz. PB8 and PB9 are 5 V tolerant, so a module with pull-ups to 5 V is fine. |
| PASS LED (green) | PB12 (CN10-16) to 330 Ω to the LED's long leg; short leg to GND | lit after a pass |
| FAULT LED (red) | PA8 to 330 Ω to the LED's long leg; short leg to GND | lit after a fault or an unknown result |
| Test button | PA10 to a push button to GND | internal pull-up, no resistor needed |
| B1 user button | PC13, on the NUCLEO | works the same as the external button |
| LD2 user LED | PA5, on the NUCLEO | on while the board applies a vector, blinks during the full check |
| Power LED | on the breadboard supply rail | shows that the breadboard has power |
| USB | ST-LINK connector on the NUCLEO | 5 V supply and the virtual COM port (USART2, PA2/PA3, 115200 baud) |

Do not wire anything else to these pins, which the board already uses: PA2 and PA3 (virtual COM port), PA5 (LD2), PA13 and PA14 (SWD debug), PC13 (B1), PH0 and PH1 (clock input from the ST-LINK).

For standalone use, power the NUCLEO from any USB power bank instead of a PC.

## Bill of materials

Prices are in INR.

| Item | Qty | Unit price | Cost |
|---|---|---|---|
| NUCLEO-F446RE development board | 1 | 3,800 | 3,800 |
| 0.96-inch SSD1306 I2C OLED | 1 | 280 | 280 |
| Breadboard (main) | 1 | 160 | 160 |
| Small breadboard (for the chip) | 1 | 20 | 20 |
| 1 kΩ resistor | 12 | 3 | 36 |
| 330 Ω resistor | 2 | 1 | 2 |
| Push button, LEDs, jumper wires | 1 set | 65 | 65 |
| Tester total | | | 4,363 |

Chips used as devices under test:

| Chip | Function | Price |
|---|---|---|
| 74HCT00 | quad 2-input NAND | 35 |
| 74HCT02 | quad 2-input NOR | 35 |
| 74HCT04 | hex inverter | 35 |
| 74HCT08 | quad 2-input AND | 40 |
| 74HCT32 | quad 2-input OR | 35 |
| 74HCT86 | quad 2-input XOR | 45 |
| Total | | 225 |

For comparison, commercial bench IC testers cost from about 9,000 INR (Kitek DICT-01) to about 35,000 INR (institutional estimate).

## Build and bring-up order

1. Place the two breadboards and wire the supply rails. Connect DIP 7 to the GND rail and DIP 14 to the 5 V rail. With a multimeter, check that the 5 V and GND rails are not shorted.
2. Wire the twelve signal lines through the 1 kΩ resistors as in the table above. Do not insert a chip yet.
3. Wire the OLED, the two LEDs with their 330 Ω resistors, and the button.
4. Flash the firmware ([FIRMWARE.md](FIRMWARE.md)). With no chip inserted, the OLED should show `READY`.
5. Unplug USB, insert a known-good 74HCT00, plug USB back in and run `python pc_tester.py --ic 74HCT00 --port COMx --verify`. All 256 vectors should match the twin. If many vectors fail, the wiring or pin order is wrong; fix that before trusting any diagnosis.
6. Repeat for the other five chips, then try standalone mode with the button.

## Safety notes

- Unplug USB before inserting or removing a chip. A chip pushed in with power on can dip the 5 V rail; the firmware re-initialises the OLED before every screen to recover from this, but swapping with power off is safer for the chip and the board.
- Check the chip orientation before powering up. A chip inserted backwards puts 5 V on its ground pin.
- Never connect DIP 7 or DIP 14 to a GPIO pin. They go to the supply rails only.
- Select the right chip on the OLED or in the PC tool before testing. The 1 kΩ resistors protect against a wrong selection, but a matching profile avoids driving outputs at all.
- Keep the power wires short and firm. A weak ground looks like a chip fault that appears only when many inputs are high.
- Re-check the rails for shorts after any wiring change.
