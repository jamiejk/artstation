#!/usr/bin/env python3
import time

import serial


PORT = "/dev/ttyACM0"


def send(port, command, delay=1.5):
    port.write(command.encode("ascii"))
    response = port.readline().decode("ascii", errors="replace").strip()
    print(f"{command.strip()} -> {response}", flush=True)
    time.sleep(delay)


def main():
    commands = [
        # Standard servo output on B1.
        "SC,4,27831\r",
        "SC,5,9855\r",
        "SC,11,2000\r",
        "SC,12,2000\r",
        "SC,8,8\r",
        "SP,0,1000,1\r",
        "SP,1,1000,1\r",
        # Narrow-band servo output on B2.
        "SC,4,12600\r",
        "SC,5,5400\r",
        "SC,8,1\r",
        "SP,0,1000,2\r",
        "SP,1,1000,2\r",
    ]

    with serial.Serial(PORT, timeout=1) as port:
        for command in commands:
            send(port, command)


if __name__ == "__main__":
    main()
