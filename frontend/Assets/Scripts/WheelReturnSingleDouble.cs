using UnityEngine;
using UnityEngine.Events;

public class WheelReturnSingleDouble : MonoBehaviour
{
    [Header("Key")]
    public KeyCode triggerKey = KeyCode.Return;

    [Header("Timing")]
    [Tooltip("Max time between presses to count as double-press.")]
    public float doublePressWindowSeconds = 0.30f;

    [Header("Callbacks")]
    public UnityEvent onSinglePress;
    public UnityEvent onDoublePress;

    [Header("Debug")]
    public bool debugLogs = true;

    private float _lastPressTime = -999f;
    private bool _pendingSingle;
    private float _pendingSingleDeadline = -1f;

    private void Update()
    {
        if (Input.GetKeyDown(triggerKey))
        {
            float now = Time.unscaledTime;

            if (now - _lastPressTime <= doublePressWindowSeconds)
            {
                _pendingSingle = false;
                _pendingSingleDeadline = -1f;
                _lastPressTime = -999f;

                if (debugLogs)
                {
                    Debug.Log($"WheelReturnSingleDouble: DOUBLE ({triggerKey})");
                }

                onDoublePress?.Invoke();
            }
            else
            {
                _lastPressTime = now;
                _pendingSingle = true;
                _pendingSingleDeadline = now + doublePressWindowSeconds;

                if (debugLogs)
                {
                    Debug.Log($"WheelReturnSingleDouble: (armed single) ({triggerKey})");
                }
            }
        }

        if (_pendingSingle && Time.unscaledTime >= _pendingSingleDeadline)
        {
            _pendingSingle = false;
            _pendingSingleDeadline = -1f;

            if (debugLogs)
            {
                Debug.Log($"WheelReturnSingleDouble: SINGLE ({triggerKey})");
            }

            onSinglePress?.Invoke();
        }
    }
}
