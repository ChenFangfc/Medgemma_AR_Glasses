using System;
using UnityEngine;

public class WheelKeyDebug : MonoBehaviour
{
    private void Update()
    {
        if (!Input.anyKeyDown)
        {
            return;
        }

        foreach (KeyCode key in Enum.GetValues(typeof(KeyCode)))
        {
            if (Input.GetKeyDown(key))
            {
                Debug.Log($"[WheelKeyDebug] KEYDOWN {key} ({(int)key})");
            }
        }
    }
}
