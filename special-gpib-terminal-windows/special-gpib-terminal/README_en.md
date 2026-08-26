# Special GPIB Terminal

This is a standalone diagnostic tool. It does not replace or modify the read-only GUI, and it has no release version number.

The terminal uses the laboratory's existing Python, PyVISA, NI-VISA, NI-488.2, and NI GPIB-USB-HS environment. It opens one VISA session, performs exactly one query or write, records the result, and closes the session.

## Start on the laboratory Windows PC

1. Extract the supplied ZIP directly under `C:\LabAutomation`.
2. Close LabVIEW VIs, NI MAX VISA test panels, and any program that is communicating with these instruments. Only one controller program should use the bus during this test.
3. Keep all source outputs off for the first query-only demonstration.
4. Double-click `START_GPIB_TERMINAL.bat`.
5. If Python is not found automatically, start PowerShell in this folder and run:

   ```powershell
   $env:PROBE_PYTHON = "C:\full\path\to\the\existing\.venv\Scripts\python.exe"
   .\START_GPIB_TERMINAL.ps1
   ```

6. At the `GPIB>` prompt, enter:

   ```text
   MAP
   LIST
   QUERY GPIB0::9::INSTR *IDN?
   ```

Expected communication display:

```text
OPEN  GPIB0::9::INSTR
TX -> *IDN?
RX <- KEITHLEY INSTRUMENTS INC.,MODEL 6221,...
CLOSE GPIB0::9::INSTR
```

`GPIB0::9::INSTR` is the VISA resource used by the host to select the device. The message placed on the GPIB bus is `*IDN?`. This is the real PyVISA form of the user's conceptual input `GPIB0::9::INSTR *IDN?`.

## Safety model

- `QUERY` is enabled by default. Known active queries such as `SENS:DATA:FRESH?` require `SEND ACTIVE_QUERY` because they can trigger or consume a reading.
- `WRITE` is locked at startup and after exit. Unlocking requires the complete phrase shown by `HELP`.
- Every write requires a second confirmation. Sourcing, output-on, triggering, reset, and TSP assignment commands require `SEND HIGH_RISK`.
- Only the six confirmed laboratory VISA resources are accepted.
- Newlines and semicolon-separated multi-command messages are blocked. One terminal line produces at most one instrument message.
- `LIST`, `MAP`, `STATUS`, `TIMEOUT`, and `CALC-R` are host-local operations. They do not send an instrument message.
- The program does not verify that the physical wiring, resistor rating, compliance, or selected current is safe. Those values require human approval.

Each session creates a new append-only JSON Lines file in `terminal_logs`. Preserve this file with the experiment record.

Use [STANDARD_INPUT_COMMANDS_en.md](STANDARD_INPUT_COMMANDS_en.md) for the English command sheet or [STANDARD_INPUT_COMMANDS_zh.md](STANDARD_INPUT_COMMANDS_zh.md) for Chinese.
