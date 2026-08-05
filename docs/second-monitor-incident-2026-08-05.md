# Second-monitor incident note

Date: 2026-08-05

## How the second monitor was used

The Alienware second monitor was **not required by Shizzle** and was never part of its cloud runtime, media delivery, processing, or monitoring architecture.

It was used accidentally because I launched headed Playwright acceptance tests on the Windows desktop. A Chromium test session and, later discovered, a WebKit window titled `karaoke-ui [WebKit]` were allowed to open on the visible desktop. That was unnecessary and contrary to the intended boundary: browser validation should have run headlessly or on cloud infrastructure. The browser was only a test consumer of `https://shizzle.systems`; nothing on the monitor or workstation supplied the application or its media.

## Diagnoses I presented too confidently

These were hypotheses, not established root causes. I should not have described them as known causes before verifying that the display recovered.

1. **Leftover Chromium automation owned the black screen.**
   - Evidence: two Playwright Chromium process trees, containing 16 automation processes, were found and terminated.
   - Why this was not the root cause: the Alienware remained black after all matched Chromium automation processes were gone.

2. **A leftover WebKit window owned the black screen.**
   - Evidence: a visible Playwright executable with the title `karaoke-ui [WebKit]` remained after the Chromium cleanup. It was terminated, and the window list then contained no Playwright/WebKit automation window.
   - Why this was not the complete root cause: the Alienware still remained black.

3. **Windows was merely showing an empty extended desktop.**
   - Action taken: I changed the Windows topology first to Extend and then to Duplicate.
   - Why this was not established: neither mode restored an image. Changing topology before reading the authoritative display configuration was premature and may have made the state harder to reason about.

4. **The graphics-output pipeline was hung.**
   - Action taken: I issued Windows' graphics-driver reset (`Win+Ctrl+Shift+B`).
   - Why this was not established: the display remained black afterward.

## What was actually observed

The strongest diagnostic evidence came from the Windows display APIs:

- The Alienware AW3423DWF was detected on DisplayPort and its EDID/mode list was readable.
- The internal Dell panel was attached to the desktop at 1920x1200/59 Hz.
- The Alienware appeared under `\\.\DISPLAY2`, `DISPLAY3`, and `DISPLAY4` aliases, but all three were detached (`StateFlags=0`) and had no current display mode.
- The monitor advertised valid modes including 3440x1440 at 60 Hz.
- A direct attempt to stage 3440x1440/60 Hz on `DISPLAY2` returned success, but querying the current mode still returned none.
- A broader `SetDisplayConfig` request with extra flags returned Windows error 87 (`ERROR_INVALID_PARAMETER`). A minimal retry was interrupted, so its outcome is unknown.

This proves only that Windows could identify the monitor while failing to attach an active desktop mode. It does **not** prove whether the underlying cause was Windows topology state, an Intel/NVIDIA driver problem, a dock/port/cable link problem, the monitor input state, or monitor hardware.

## What I would try next

These are ordered recovery hypotheses, not claimed root causes:

1. **Reboot first.** Stop making display changes and let Windows, Intel/NVIDIA graphics, and the DisplayPort link rebuild their state from boot.
2. **If still black, verify the physical signal path independently of Windows:** use the monitor's OSD to select the actual connected DisplayPort input, run its built-in self-test, power-cycle it, and reseat or replace the DisplayPort cable/dock connection.
3. **Use Windows Display Settings manually:** select the detected Alienware, choose “Extend desktop to this display,” start at 3440x1440/60 Hz, and temporarily disable HDR/variable refresh while testing.
4. **If Windows still detects but will not attach it:** disable/re-enable the relevant display adapter and monitor device, then install the Dell/OEM Intel and NVIDIA graphics packages rather than continuing to force topology through ad-hoc API calls.
5. **If another cable/port or another source also produces black:** treat the monitor/input hardware as the leading suspect and use the Alienware diagnostics or Dell support path.

## Process correction

Future Shizzle browser acceptance must be headless and cloud-hosted. No local headed browser, monitor enumeration, or display-topology change is part of the project workflow. For hardware incidents, distinguish observations from hypotheses, obtain confirming evidence after every action, and never call a hypothesis the root cause merely because a related process or setting was found.
