using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Android;

public class AndroidRuntimePermissions : MonoBehaviour
{
    private const string CameraPermission = "android.permission.CAMERA";
    private const string MicPermission = "android.permission.RECORD_AUDIO";
    private const string BluetoothConnectPermission = "android.permission.BLUETOOTH_CONNECT";
    private const string BluetoothScanPermission = "android.permission.BLUETOOTH_SCAN";

    private void Start()
    {
        RequestRequiredPermissions();
    }

    public void RequestRequiredPermissions()
    {
#if UNITY_ANDROID && !UNITY_EDITOR
        List<string> permissions = new List<string>
        {
            CameraPermission,
            MicPermission
        };

        int sdkInt = GetAndroidSdkInt();
        if (sdkInt >= 31)
        {
            permissions.Add(BluetoothConnectPermission);
            permissions.Add(BluetoothScanPermission);
        }

        foreach (string permission in permissions)
        {
            if (!Permission.HasUserAuthorizedPermission(permission))
            {
                Permission.RequestUserPermission(permission);
            }
        }
#endif
    }

    private int GetAndroidSdkInt()
    {
#if UNITY_ANDROID && !UNITY_EDITOR
        using (AndroidJavaClass version = new AndroidJavaClass("android.os.Build$VERSION"))
        {
            return version.GetStatic<int>("SDK_INT");
        }
#else
        return 0;
#endif
    }
}
