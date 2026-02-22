using System;
using UnityEngine;

public class WheelKeyFinder : MonoBehaviour
{
    // Set true to also log key UP (usually not needed)
    public bool logKeyUp = false;

    private void Update()
    {
        // Scan ALL KeyCode values and log the first one pressed this frame.
        // This is only for debugging, you can remove it after you learn the real key.
        foreach (KeyCode k in Enum.GetValues(typeof(KeyCode)))
        {
            if (Input.GetKeyDown(k))
            {
                Debug.Log($"WheelKeyFinder: KEYDOWN = {k}");
                break;
            }

            if (logKeyUp && Input.GetKeyUp(k))
            {
                Debug.Log($"WheelKeyFinder: KEYUP = {k}");
                break;
            }
        }
    }
}
