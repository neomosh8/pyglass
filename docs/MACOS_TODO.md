# macOS port TODO — live **and** recordable desktop glass

On Windows the desktop pane is now live + flicker-free + smooth-drag + **recordable**
+ no self-capture, via the **Magnification API** ([`pyglass/_magnifier.py`](../pyglass/_magnifier.py)):
`MagSetWindowFilterList(MW_FILTERMODE_EXCLUDE, [glass])` excludes the glass from
*our* capture only (so it isn't in its own backdrop) while staying visible to
Snipping Tool / OBS.

macOS today only does half of that: [`ScreenBackdrop`](../pyglass/backdrop.py)
captures with the `screencapture` CLI and hides the window from capture *globally*
via `NSWindowSharingNone` (`exclude_from_capture`). So it's live but **not
recordable**; the `C` key only trades that for a paused-but-recordable mode.

**Goal:** the macOS equivalent of the Windows win — capture the live screen with
the glass excluded from *our* stream only, so it's live **and** recordable. The
correct API is **ScreenCaptureKit** (`SCContentFilter(display:excludingWindows:)`,
macOS 12.3+) — the per-stream analog of the magnifier filter.

### Key learnings from the Windows implementation (apply these)
1. **Per-capture exclusion, not global.** `excludingWindows:` is per-stream, like
   `MagSetWindowFilterList` — that's what keeps it recordable. Do **not** use
   `NSWindowSharingNone` for the live path.
2. **Re-assert the exclusion.** On Windows the filter had to be re-applied every
   grab or the glass leaked into its own backdrop and piled up ("hall of mirrors")
   while stationary. Watch for the same on SCK — if the window's `CGWindowID`/
   handle changes, rebuild the `SCContentFilter`. Test the stationary case explicitly.
3. **Cheap per-frame.** Skip the refract when the captured region is unchanged
   (already in `_ingest_array`), and on drag grab once + re-slice (`prepare_drag`/
   `end_drag`). SCK is push-based, so "grab" = take the latest delivered frame.

---

## Tasks (in order)

### 1. Generalize the capturer hook in `ScreenBackdrop`
[`pyglass/backdrop.py`] — Right now the Windows magnifier is special-cased as
`self._mag` with `_grab_mag()`. Rename to a platform-neutral `self._capturer`
(any object with `grab(x,y,w,h)->ndarray|None`, `set_exclude(widget)`, `close()`)
and `_grab_capturer()`. Route `refresh()`, `_start_grab()`, `prepare_drag()`,
`cleanup()`, and the `recordable`/`capturable` properties through it.
**Done when:** Windows still works unchanged with `_capturer` being the magnifier.

### 2. New `pyglass/_screencapturekit.py` (analog of `_magnifier.py`)
A `ScreenCaptureKitCapture` class + `available()`:
- `available()` → `True` on macOS ≥ 12.3 with the PyObjC ScreenCaptureKit
  framework importable.
- `__init__(exclude_widget)`:
  - `SCShareableContent.getShareableContentWithCompletionHandler:` (it's async —
    block on a small run-loop/semaphore to get `displays` + `windows`).
  - Find the `SCWindow` whose `windowID` == the glass's `CGWindowID` (see task 3).
  - `SCContentFilter alloc initWithDisplay:display excludingWindows:[scWindow]`.
  - `SCStreamConfiguration`: `width`/`height` = display physical px (point size ×
    `backingScaleFactor`), `pixelFormat` = `'BGRA'` (kCVPixelFormatType_32BGRA),
    `minimumFrameInterval` ~ 1/15 s, `queueDepth` small, `showsCursor` as desired.
  - `SCStream initWithFilter:configuration:delegate:`, add an `SCStreamOutput`
    delegate on `.screen`, `startCaptureWithCompletionHandler:`.
  - The delegate stores only the **latest** `CMSampleBuffer`'s `CVPixelBuffer`.
- `grab(x, y, w, h)` → take the latest `CVPixelBuffer`, lock base address, read
  `bytesPerRow` (stride) × height of BGRA, crop to the requested rect, swap to
  RGBA, return `(h, w, 4)` uint8 (matching `qimage_to_array`). Return `None` if no
  frame yet.
- `set_exclude(widget)` → rebuild the `SCContentFilter` for the (possibly new)
  window and `stream.updateContentFilter:completionHandler:`. (Mirrors the
  Windows re-assert; call it from `grab` if a stationary feedback test shows leakage.)
- `close()` → `stream.stopCaptureWithCompletionHandler:` and release.
**Done when:** a standalone probe captures the screen with a chosen window absent.

### 3. Map a Qt widget → `SCWindow`
[`_screencapturekit.py`] — From `widget.winId()` (an `NSView*`), get
`view.window()` (`NSWindow`), then `nswindow.windowNumber()` → that's the
`CGWindowID` to match against `SCShareableContent.windows[].windowID`. Reuse the
ctypes-objc pattern already in `exclude_from_capture` (or do it in PyObjC).
**Done when:** the right `SCWindow` is found and excluded (verify in task 8).

### 4. Wire ScreenCaptureKit into `configure()`
[`pyglass/backdrop.py` `ScreenBackdrop.configure`] — Add a macOS branch **before**
the `exclude_from_capture` fallback, parallel to the existing win32/magnifier one:
try `ScreenCaptureKitCapture(self._widget)`; on success set `self._capturer`,
`self._excluded = True`, return `True`. Otherwise fall back to the current
`screencapture` CLI + `NSWindowSharingNone` path (macOS < 12.3 / no PyObjC).
**Done when:** on a 12.3+ Mac the pane uses SCK and `recordable` is `True`.

### 5. Cadence + crop
[`pyglass/backdrop.py`] — When the capturer is active, use the fast interval
(~120 ms) like Windows, not the 900 ms `screencapture` cadence. SCK delivers
frames continuously; the timer just reads the latest. Crop to the region behind
the panel (the `_capture_rect()` logic) in numpy — or, optionally, drive
`changed` straight from the stream-output delegate for true push updates.
**Done when:** minimize-an-app latency feels instant and CPU is reasonable.

### 6. `recordable` / `capturable` / `C` toggle
[`pyglass/backdrop.py`, `pyglass/desktop.py` hint] — With SCK the window is
recordable while live, so `recordable` should be `True` and the `C` toggle is a
no-op (same as the magnifier path). Keep `C` meaningful only for the
`NSWindowSharingNone` fallback. Update `_update_hint()` wording for macOS.
**Done when:** the hint shows "live ● · recordable" on a 12.3+ Mac.

### 7. Dependency + packaging
[`pyproject.toml`] — ScreenCaptureKit via **PyObjC** (pure ctypes-objc is
impractical for the async stream + delegates). Add an optional extra, e.g.
`[project.optional-dependencies] macos = ["pyobjc-framework-ScreenCaptureKit",
"pyobjc-framework-Quartz"]`, and import it lazily so non-mac installs are
unaffected. Document `pip install "pyglass-qt[macos]"`.
**Done when:** import is lazy; Windows/Linux installs don't pull PyObjC.

### 8. Verify on the Mac (mirror the Windows probes)
- **Exclusion:** SCK stream omits the glass while a separate `screencapture`/
  `CGWindowListCreateImage` still shows it (per-capture, not global).
- **Stationary self-capture / feedback:** over a static wallpaper, consecutive
  captured frames differ by ~0 (no "hall of mirrors"); if not, re-assert the
  filter per grab (task 2).
- **Latency:** minimize an app behind it → glass updates within a frame or two.
- **Recordable:** a QuickTime/macOS screen recording shows the glass live.
- **Drag** stays smooth; **dials** (`[ ]`, `- +`), **flicker-free**, `R`/`Esc` work.

### 9. Docs
[`README.md`] — Update the "Desktop mode" macOS bullet: live **and** recordable
via ScreenCaptureKit (12.3+), `screencapture` + `NSWindowSharingNone` as the
fallback. Note the `[macos]` extra and Screen Recording permission.

---

## Already cross-platform — just confirm on Mac (no code change expected)
- Refraction engine, dials, the flicker fix (timer gated on `_excluded`), smooth
  drag (`prepare_drag`/`end_drag`), in-app `GlassPane` modals (`WidgetBackdrop`),
  the multi-monitor `virtualGeometry` anchoring, DPI/Retina handling.

## Permissions
ScreenCaptureKit needs **Screen Recording** permission (System Settings →
Privacy & Security → Screen Recording) for the app running Python — same as the
`screencapture` CLI. First run prompts; may need a relaunch after granting.

## Open questions to resolve while implementing
- Does the `SCContentFilter` exclusion need re-asserting per frame (like the
  magnifier) or is it stable for the stream's life? (Test the stationary case.)
- Retina: confirm `SCStreamConfiguration.width/height` should be physical px and
  that the resulting `dpr` (physical/point) feeds the kernel correctly.
- Multi-display: which `SCDisplay` to capture when the pane spans/moves monitors;
  re-create the filter on `screenChanged`.
