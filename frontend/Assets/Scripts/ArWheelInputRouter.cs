using System;
using System.Collections;
using UnityEngine;

public class ArWheelInputRouter : MonoBehaviour
{
    [Header("DigiOS Wheel Key Mapping")]
    [Tooltip("Primary wheel press mapping from DigiOS docs.")]
    [SerializeField] private KeyCode primaryPressKey = KeyCode.Return;
    [Tooltip("Optional fallback press key. Leave as None if unknown.")]
    [SerializeField] private KeyCode secondaryPressKey = KeyCode.None;
    [Tooltip("DigiOS long-press key mapping from the wheel.")]
    [SerializeField] private KeyCode menuLongPressKey = KeyCode.Menu;
    [Tooltip("On ARGO wheel double-click is often delivered as Escape.")]
    [SerializeField] private KeyCode escapeDoublePressKey = KeyCode.Escape;
    [SerializeField] private KeyCode forwardScrollKey = KeyCode.RightArrow;
    [SerializeField] private KeyCode backwardScrollKey = KeyCode.LeftArrow;

    [Header("Press Timing")]
    [Tooltip("Disable if another script handles single/double/long press.")]
    [SerializeField] private bool enablePressDetection = true;
    [Tooltip("If true, Menu key down fires LongPress immediately (recommended on ARGO).")]
    [SerializeField] private bool useMenuKeyForLongPress = true;
    [Tooltip("Optional fallback: detect long-press by holding press key.")]
    [SerializeField] private bool enableHoldLongPressFallback = false;
    [Tooltip("Long press duration for recording toggle.")]
    [SerializeField] private float longPressSeconds = 2f;
    [Tooltip("Second-press timing window for double press detection.")]
    [SerializeField] private float doublePressWindowSeconds = 0.6f;

    [Header("Editor Fallback")]
    [SerializeField] private bool enableKeyboardFallback = true;
    [SerializeField] private bool enableMouseWheelFallback = true;
    [SerializeField] private float mouseWheelScale = 1f;
    [SerializeField] private bool debugLogs = true;

    private bool _pressHeld;
    private bool _longPressFired;
    private float _pressBeganAt;
    private float _lastPressTime = -999f;
    private Coroutine _pendingSingleCoroutine;

    public event Action SinglePress;
    public event Action DoublePress;
    public event Action LongPress;
    public event Action<float> Scroll;

    private void Update()
    {
        if (enableKeyboardFallback)
        {
            PollFallbackInput();
        }
    }

    private void PollFallbackInput()
    {
        if (enableMouseWheelFallback)
        {
            float wheel = Input.mouseScrollDelta.y * mouseWheelScale;
            if (Mathf.Abs(wheel) > Mathf.Epsilon)
            {
                Log($"Mouse wheel delta detected: {wheel:0.###}");
                RaiseScroll(wheel);
            }
        }

        if (Input.GetKeyDown(forwardScrollKey))
        {
            // Forward wheel scroll: move toward later content.
            Log($"Forward scroll key detected ({forwardScrollKey}).");
            RaiseScroll(-1f);
        }

        if (Input.GetKeyDown(backwardScrollKey))
        {
            // Backward wheel scroll: move toward earlier content.
            Log($"Backward scroll key detected ({backwardScrollKey}).");
            RaiseScroll(1f);
        }

        if (enablePressDetection
            && useMenuKeyForLongPress
            && menuLongPressKey != KeyCode.None
            && Input.GetKeyDown(menuLongPressKey))
        {
            CancelPendingSingle();
            _pressHeld = false;
            _longPressFired = false;
            _lastPressTime = -999f;
            Log($"Menu long-press key detected ({menuLongPressKey}).");
            RaiseLongPress();
        }

        if (enablePressDetection
            && escapeDoublePressKey != KeyCode.None
            && Input.GetKeyDown(escapeDoublePressKey))
        {
            CancelPendingSingle();
            _pressHeld = false;
            _longPressFired = false;
            _lastPressTime = -999f;
            Log($"Escape double-press key detected ({escapeDoublePressKey}).");
            RaiseDoublePress();
            return;
        }

        if (enablePressDetection && TryGetPressKeyDown(out KeyCode keyDown))
        {
            Log($"Press key down detected ({keyDown}).");
            _pressHeld = true;
            _longPressFired = false;
            _pressBeganAt = Time.unscaledTime;

            float now = Time.unscaledTime;
            if (now - _lastPressTime <= doublePressWindowSeconds)
            {
                CancelPendingSingle();
                _lastPressTime = -999f;
                Log("Detected double press.");
                RaiseDoublePress();
            }
            else
            {
                _lastPressTime = now;
                CancelPendingSingle();
                _pendingSingleCoroutine = StartCoroutine(FireSingleAfterWindow(now));
            }
        }

        if (enablePressDetection
            && enableHoldLongPressFallback
            && _pressHeld
            && !_longPressFired
            && Time.unscaledTime - _pressBeganAt >= longPressSeconds)
        {
            _longPressFired = true;
            _lastPressTime = -999f;
            CancelPendingSingle();
            Log("Long press threshold reached.");
            RaiseLongPress();
        }

        if (enablePressDetection && TryGetPressKeyUp(out KeyCode keyUp))
        {
            Log($"Press key up detected ({keyUp}).");
            float heldSeconds = Time.unscaledTime - _pressBeganAt;
            Log($"Press held duration: {heldSeconds:0.###}s");
            _pressHeld = false;

            if (_longPressFired)
            {
                _longPressFired = false;
            }
        }
    }

    // Use these methods to bind your glasses wheel hardware callbacks.
    public void RaiseSinglePress()
    {
        Log("RaiseSinglePress fired.");
        SinglePress?.Invoke();
    }

    public void RaiseDoublePress()
    {
        Log("RaiseDoublePress fired.");
        DoublePress?.Invoke();
    }

    public void RaiseLongPress()
    {
        Log("RaiseLongPress fired.");
        LongPress?.Invoke();
    }

    public void RaiseScroll(float delta)
    {
        Log($"RaiseScroll fired with delta={delta:0.###}.");
        Scroll?.Invoke(delta);
    }

    private IEnumerator FireSingleAfterWindow(float firstPressTime)
    {
        yield return new WaitForSecondsRealtime(doublePressWindowSeconds);

        // If the key is still held, wait for release or long-press.
        while (_pressHeld && !_longPressFired)
        {
            yield return null;
        }

        if (!_longPressFired && Mathf.Abs(_lastPressTime - firstPressTime) < 0.0001f)
        {
            Log("Detected single press.");
            RaiseSinglePress();
        }

        _pendingSingleCoroutine = null;
    }

    private void CancelPendingSingle()
    {
        if (_pendingSingleCoroutine == null)
        {
            return;
        }

        StopCoroutine(_pendingSingleCoroutine);
        _pendingSingleCoroutine = null;
    }

    private bool TryGetPressKeyDown(out KeyCode key)
    {
        if (Input.GetKeyDown(primaryPressKey))
        {
            key = primaryPressKey;
            return true;
        }

        if (secondaryPressKey != primaryPressKey && Input.GetKeyDown(secondaryPressKey))
        {
            key = secondaryPressKey;
            return true;
        }

        key = KeyCode.None;
        return false;
    }

    private bool TryGetPressKeyUp(out KeyCode key)
    {
        if (Input.GetKeyUp(primaryPressKey))
        {
            key = primaryPressKey;
            return true;
        }

        if (secondaryPressKey != primaryPressKey && Input.GetKeyUp(secondaryPressKey))
        {
            key = secondaryPressKey;
            return true;
        }

        key = KeyCode.None;
        return false;
    }

    private void OnDisable()
    {
        CancelPendingSingle();
        _pressHeld = false;
        _longPressFired = false;
        _lastPressTime = -999f;
    }

    private void Log(string message)
    {
        if (!debugLogs)
        {
            return;
        }

        Debug.Log($"[ArWheelInputRouter] {message}");
    }
}
