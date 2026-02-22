# Unity AR Clinical Assistant - Setup Notes

This project now includes a baseline client for your requirements in:

- `Assets/Scripts/ArGlassesClinicalAssistant.cs`
- `Assets/Scripts/ArDigiOSVoiceUiBridge.cs`
- `Assets/Scripts/ArWorkflowWebSocket.cs`
- `Assets/Scripts/ArAudioRecorder.cs`
- `Assets/Scripts/ArWheelInputRouter.cs`
- `Assets/Scripts/ArGlassesUiPresenter.cs`
- `Assets/Scripts/AndroidRuntimePermissions.cs`

## 1. Scene Setup

1. Create one root GameObject (example: `ARClinicalAssistant`).
2. Add these components to it:
   - `ArGlassesClinicalAssistant`
   - `ArDigiOSVoiceUiBridge`
   - `ArWorkflowWebSocket`
   - `ArAudioRecorder`
   - `ArWheelInputRouter`
   - `AndroidRuntimePermissions`
3. Add an `AudioSource` component on the same object (for start/stop earcons).
4. Add `ArGlassesUiPresenter` to a UI object, then assign it to `ArGlassesClinicalAssistant`.

## 2. UI Wiring

In `ArGlassesUiPresenter`, bind these text targets (either Unity `Text` or `TMP_Text`):

- Header text: `NOTE (SHORT)` / `NOTE (FULL)` / `ADVICE (...)`
- Body text: current content
- Status text: `Processing...`, timeout, failures
- Connectivity text: `Online` / `Offline`
- Recording text: `REC mm:ss`

Optional:

- Assign a `CanvasGroup` for pulsing recording icon/element.
- For long full-view content, assign `Body Scroll Rect` in `ArGlassesUiPresenter`.
  - Wheel scroll will drive `ScrollRect.verticalNormalizedPosition`.
  - TMP body text is configured in code to `Overflow`.

## 3. Server Endpoint

In `ArGlassesClinicalAssistant`:

- Set `Workflow Url` to your server:
  - `ws://<server-ip>:8003/ws`
- For ngrok:
  - `wss://<your-subdomain>.ngrok-free.dev/ws`
- `Patient Id` is now auto-generated per app launch by default (`generatePatientIdPerLaunch`).

Important:

- Do not use `ws://127.0.0.1:8003/ws` on ARGO unless you are using `adb reverse`.
- On-device, `127.0.0.1` points to the glasses itself, not your workstation/server.
- Workflow URL should include `/ws` path.
- If server reports `unknown session_id` (for example after backend restart), client now invalidates local session, sends a new `start_session`, and retries buffered audio automatically.

## 4. Input Mapping

Required mapping is implemented:

- Long press (`Menu` key): start/stop recording
- Single press (`Return`): toggle view
  - `NOTE_FULL <-> ADVICE_FULL`
- Double press / ARGO `Escape`: quit app (best-effort `end_session` before quit)
- Scroll: scroll current view

The wheel key mapping follows DigiOS Unity guidance:

- Forward scroll: `RightArrow`
- Backward scroll: `LeftArrow`
- Wheel press: `Return`

For quick editor testing:

- Press `Return` for wheel press logic
- Press `LeftArrow`/`RightArrow` for backward/forward scroll
- Mouse wheel fallback is also enabled by default

For glasses hardware integration:

- Call `ArWheelInputRouter.RaiseSinglePress()`
- Call `ArWheelInputRouter.RaiseDoublePress()`
- Call `ArWheelInputRouter.RaiseLongPress()`
- Call `ArWheelInputRouter.RaiseScroll(delta)`

Press detection details in `ArWheelInputRouter`:

- Single vs double press uses a delayed-single window (`doublePressWindowSeconds`).
- Long press defaults to DigiOS `Menu` key-down (`menuLongPressKey`), with optional hold fallback.
- Wheel press uses `primaryPressKey` (default `Return`) and optional `secondaryPressKey` (default `None`).

Optional key-mapping probe:

- Attach `WheelKeyDebug` to any active GameObject and check logs to see which `KeyCode` ARGO sends.

## 5. Smoke Tests from Terminal

Reusable scripts are available in `scripts/`:

- `scripts/workflow_smoke_test.py`
  - Protocol-level workflow test: `start_session -> process_audio -> get_latest`
  - Example:
    - `python3 scripts/workflow_smoke_test.py --ws-url ws://<server-ip>:8003/ws --end-session`
- `scripts/workflow_chunked_smoke_test.py`
  - Protocol-level chunked test: `start_session -> audio_begin -> audio_chunk* -> audio_end`
  - Example:
    - `python3 scripts/workflow_chunked_smoke_test.py --ws-url ws://<server-ip>:8003/ws --chunk-ms 500`
- `scripts/check_ws_services.sh`
  - Service health check for backend model workflow services
  - Example:
    - `bash scripts/check_ws_services.sh --user`
- `scripts/adb_runtime_smoke.sh`
  - Installs APK (optional), launches app, sends key events (optional), captures filtered logs
  - Example:
    - `bash scripts/adb_runtime_smoke.sh --apk test_v1.apk --auto-keys`
- `scripts/run_all_smoke_tests.sh`
  - Wrapper for running all selected checks
  - Example:
    - `bash scripts/run_all_smoke_tests.sh --ws-url ws://<server-ip>:8003/ws --apk test_v1.apk`

`run_all_smoke_tests.sh` supports `--skip-workflow`, `--skip-services`, `--skip-adb` for partial runs.

## 6. Runtime Permissions

`AndroidRuntimePermissions` requests:

- Camera
- Microphone
- Bluetooth runtime permissions on Android 12+

Keep this component in your first scene so permission prompts occur on startup.

## 7. DigiOS Input Compatibility

Per DigiOS docs, scroll-wheel key events require Old Input System or Both:

- Unity Player Settings > Other Settings > Active Input Handling
- Current project value is compatible (`activeInputHandler: 0`, Old Input System)

## 8. Voice UI Manifest Notes

Manifest now includes:

- `<queries>` entry for `com.digilens.digios_voiceui_service`
- `com.digilens.android.BUILT_FOR_GLASSES`
- `com.digilens.dynamic_display = s3d`
- `com.digilens.dynamic_display_fsd = rgb`

Voice UI service entry is already present in `Assets/Plugins/Android/AndroidManifest.xml`.

The VoiceUI Android plugin is installed at:

- `Assets/Plugins/Android/DigiOS-Unity-Plugin-release.aar`

## 9. Voice Command Mapping

`ArDigiOSVoiceUiBridge` default commands:

- `start recording`
- `stop recording`
- `toggle recording`
- `show note`
- `show advice`
- `show short`
- `show full`
- `retry upload`

All phrases are editable in the Inspector.

The bridge also sets the callback object name to `VoiceUI_Handler` by default (editable in Inspector), matching DigiOS examples.
