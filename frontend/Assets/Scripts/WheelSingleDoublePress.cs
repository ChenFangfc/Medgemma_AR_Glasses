using System;
using System.Collections;
using UnityEngine;
using UnityEngine.Events;

public class WheelSingleDoublePress : MonoBehaviour
{
    [Header("Primary press key (your wheel click): set to Return")]
    public KeyCode pressKey = KeyCode.Return;

    [Header("Scroll keys (your wheel roll): Left/Right arrows")]
    public KeyCode scrollLeftKey = KeyCode.LeftArrow;
    public KeyCode scrollRightKey = KeyCode.RightArrow;

    [Header("Optional secondary keys seen on ARGO (system-level mappings)")]
    public KeyCode menuKey = KeyCode.Menu;     // often appears on long/double OS actions
    public KeyCode escapeKey = KeyCode.Escape; // back

    [Header("Double press timing window")]
    public float doubleWindowSeconds = 0.30f;

    [Header("Long press (only works if Unity receives a held pressKey)")]
    public float longPressSeconds = 2.0f;

    [Header("Debug logs")]
    public bool debugLogs = true;

    [Serializable] public class IntEvent : UnityEvent<int> {}

    // Events you can wire in Inspector later (no method-name guessing!)
    public UnityEvent onSinglePress;
    public UnityEvent onDoublePress;
    public UnityEvent onLongPress;
    public UnityEvent onMenuKey;
    public UnityEvent onEscapeKey;
    public IntEvent onScroll; // +1 = right/down, -1 = left/up

    private float lastPressTime = -999f;
    private Coroutine pendingSingle = null;

    private bool longFired = false;
    private float pressDownTime = -1f;

    void Update()
    {
        // --- SCROLL ---
        if (Input.GetKeyDown(scrollLeftKey))
        {
            if (debugLogs) Debug.Log("Wheel: SCROLL -1 (LeftArrow)");
            onScroll?.Invoke(-1);
        }
        if (Input.GetKeyDown(scrollRightKey))
        {
            if (debugLogs) Debug.Log("Wheel: SCROLL +1 (RightArrow)");
            onScroll?.Invoke(+1);
        }

        // --- MENU / ESCAPE (often OS mapped) ---
        if (Input.GetKeyDown(menuKey))
        {
            if (debugLogs) Debug.Log("Wheel: MENU key down");
            onMenuKey?.Invoke();
        }
        if (Input.GetKeyDown(escapeKey))
        {
            if (debugLogs) Debug.Log("Wheel: ESCAPE key down");
            onEscapeKey?.Invoke();
        }

        // --- PRESS (single/double + optional long) ---
        if (Input.GetKeyDown(pressKey))
        {
            float now = Time.unscaledTime;
            pressDownTime = now;
            longFired = false;

            // Double press detection: two pressKey downs within window
            if (now - lastPressTime <= doubleWindowSeconds)
            {
                if (pendingSingle != null)
                {
                    StopCoroutine(pendingSingle);
                    pendingSingle = null;
                }

                if (debugLogs) Debug.Log("Wheel: DOUBLE (Return x2)");
                onDoublePress?.Invoke();
                lastPressTime = -999f;
            }
            else
            {
                lastPressTime = now;

                if (pendingSingle != null)
                {
                    StopCoroutine(pendingSingle);
                    pendingSingle = null;
                }

                pendingSingle = StartCoroutine(FireSingleAfterWindow(now));
            }
        }

        // Long press detection (only works if Unity delivers pressKey as held)
        if (!longFired && pressDownTime > 0 && Input.GetKey(pressKey))
        {
            float held = Time.unscaledTime - pressDownTime;
            if (held >= longPressSeconds)
            {
                longFired = true;
                if (pendingSingle != null)
                {
                    StopCoroutine(pendingSingle);
                    pendingSingle = null;
                }
                if (debugLogs) Debug.Log($"Wheel: LONG (held {held:F2}s)");
                onLongPress?.Invoke();
            }
        }

        if (Input.GetKeyUp(pressKey))
        {
            pressDownTime = -1f;
        }
    }

    private IEnumerator FireSingleAfterWindow(float firstPressTime)
    {
        yield return new WaitForSecondsRealtime(doubleWindowSeconds);

        if (Mathf.Abs(lastPressTime - firstPressTime) < 0.0001f)
        {
            if (debugLogs) Debug.Log("Wheel: SINGLE (Return)");
            onSinglePress?.Invoke();
        }

        pendingSingle = null;
    }
}
