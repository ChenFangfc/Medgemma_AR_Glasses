using System;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.UI;
#if UNITY_ANDROID
using UnityEngine.Android;
#endif

public class ArPhotoCapture : MonoBehaviour
{
    [Header("Camera")]
    [SerializeField] private bool preferRearCamera = true;
    [SerializeField] [Range(0, 20)] private int warmupFrames = 8;
    [SerializeField] [Range(0.2f, 6f)] private float warmupTimeoutSeconds = 2.5f;
    [SerializeField] private int previewRequestWidth = 1280;
    [SerializeField] private int previewRequestHeight = 720;
    [SerializeField] [Range(5, 60)] private int previewRequestFps = 30;

    [Header("Preview")]
    [SerializeField] private RawImage previewRawImage;
    [SerializeField] private bool autoCreatePreviewSurface = true;
    [SerializeField] private bool hidePreviewWhenIdle = true;

    [Header("Debug")]
    [SerializeField] private bool debugLogs = true;

    private WebCamTexture _previewWebcam;
    private Texture2D _stillTexture;
    private bool _previewStarting;

    public int LastCaptureWidth { get; private set; }
    public int LastCaptureHeight { get; private set; }
    public string LastCaptureMimeType { get; private set; } = "image/jpeg";

    public bool IsPreviewRunning => _previewWebcam != null && _previewWebcam.isPlaying;

    public async Task<bool> StartPreviewAsync()
    {
        if (_previewStarting)
        {
            float waitDeadline = Time.realtimeSinceStartup + 3f;
            while (_previewStarting && Time.realtimeSinceStartup < waitDeadline)
            {
                await Task.Yield();
            }

            return IsPreviewRunning;
        }

        if (IsPreviewRunning)
        {
            AttachPreviewTexture();
            return true;
        }

        ClearStillPreview();

        _previewStarting = true;
        try
        {
            bool permissionGranted = await EnsureCameraPermissionAsync();
            if (!permissionGranted)
            {
                Log("[CAM] permission denied");
                return false;
            }

            WebCamDevice[] devices = WebCamTexture.devices;
            if (devices == null || devices.Length == 0)
            {
                Log("[CAM] no camera devices found");
                return false;
            }

            Log($"[CAM] webcam devices count={devices.Length}");
            int selectedIndex = ChooseDeviceIndex(devices);
            WebCamDevice selected = devices[selectedIndex];
            Log($"[CAM] selected device={selected.name}, front={selected.isFrontFacing}");

            _previewWebcam = new WebCamTexture(
                selected.name,
                Mathf.Max(320, previewRequestWidth),
                Mathf.Max(240, previewRequestHeight),
                Mathf.Clamp(previewRequestFps, 5, 60));

            _previewWebcam.Play();

            bool ready = await WaitForWebcamReadyAsync(_previewWebcam);
            if (!ready)
            {
                Log("[CAM] webcam warmup timeout");
                StopPreview();
                return false;
            }

            AttachPreviewTexture();
            return true;
        }
        catch (Exception ex)
        {
            Debug.LogWarning($"[CAM] start preview exception: {ex.Message}");
            StopPreview();
            return false;
        }
        finally
        {
            _previewStarting = false;
        }
    }

    public void StopPreview()
    {
        if (_previewWebcam != null)
        {
            if (_previewWebcam.isPlaying)
            {
                _previewWebcam.Stop();
            }

            Destroy(_previewWebcam);
            _previewWebcam = null;
        }

        ClearStillPreview();

        if (previewRawImage != null)
        {
            previewRawImage.texture = null;
            previewRawImage.enabled = false;
            if (hidePreviewWhenIdle)
            {
                previewRawImage.gameObject.SetActive(false);
            }
        }
    }

    public bool ShowCapturedImage(byte[] imageBytes)
    {
        if (imageBytes == null || imageBytes.Length == 0)
        {
            return false;
        }

        if (!EnsurePreviewSurface())
        {
            return false;
        }

        if (_previewWebcam != null)
        {
            if (_previewWebcam.isPlaying)
            {
                _previewWebcam.Stop();
            }

            Destroy(_previewWebcam);
            _previewWebcam = null;
        }

        ClearStillPreview();
        Texture2D tex = new Texture2D(2, 2, TextureFormat.RGB24, false);
        bool loaded = tex.LoadImage(imageBytes, markNonReadable: false);
        if (!loaded)
        {
            Destroy(tex);
            Log("[CAM] failed to decode captured image for preview");
            return false;
        }

        _stillTexture = tex;
        previewRawImage.texture = _stillTexture;
        previewRawImage.enabled = true;
        previewRawImage.gameObject.SetActive(true);
        (previewRawImage.transform as RectTransform)?.SetAsFirstSibling();
        Log($"[CAM] show captured image preview size={_stillTexture.width}x{_stillTexture.height}");
        return true;
    }

    public async Task<byte[]> CaptureJpegAsync(int maxDim = 1024, int jpegQuality = 80)
    {
        LastCaptureWidth = 0;
        LastCaptureHeight = 0;
        LastCaptureMimeType = "image/jpeg";

        bool hadPreview = IsPreviewRunning;
        if (!hadPreview)
        {
            bool previewStarted = await StartPreviewAsync();
            if (!previewStarted)
            {
                return null;
            }
        }

        Texture2D sourceTexture = null;
        Texture2D outputTexture = null;

        try
        {
            if (!IsPreviewRunning || _previewWebcam == null || _previewWebcam.width <= 16 || _previewWebcam.height <= 16)
            {
                Log("[CAM] capture failed: preview not ready");
                return null;
            }

            sourceTexture = new Texture2D(_previewWebcam.width, _previewWebcam.height, TextureFormat.RGB24, false);
            sourceTexture.SetPixels32(_previewWebcam.GetPixels32());
            sourceTexture.Apply(updateMipmaps: false, makeNoLongerReadable: false);

            outputTexture = ResizeToMaxDimension(sourceTexture, Mathf.Max(256, maxDim), out int outWidth, out int outHeight);
            byte[] jpeg = outputTexture.EncodeToJPG(Mathf.Clamp(jpegQuality, 40, 100));
            if (jpeg == null || jpeg.Length == 0)
            {
                Log("[CAM] jpeg encode failed");
                return null;
            }

            LastCaptureWidth = outWidth;
            LastCaptureHeight = outHeight;
            LastCaptureMimeType = "image/jpeg";

            Log($"[CAM] capture ok bytes={jpeg.Length} size={outWidth}x{outHeight}");
            return jpeg;
        }
        catch (Exception ex)
        {
            Debug.LogWarning($"[CAM] capture exception: {ex.Message}");
            return null;
        }
        finally
        {
            if (sourceTexture != null && !ReferenceEquals(sourceTexture, outputTexture))
            {
                Destroy(sourceTexture);
            }

            if (outputTexture != null)
            {
                Destroy(outputTexture);
            }

            if (!hadPreview)
            {
                StopPreview();
            }
        }
    }

    public async void DebugCaptureOnce()
    {
        byte[] bytes = await CaptureJpegAsync();
        if (bytes == null || bytes.Length == 0)
        {
            Log("[CAM] debug capture failed");
            return;
        }

        Log($"[CAM] debug capture bytes={bytes.Length}");
    }

    private void AttachPreviewTexture()
    {
        if (_previewWebcam == null)
        {
            return;
        }

        if (!EnsurePreviewSurface())
        {
            return;
        }

        ClearStillPreview();

        previewRawImage.texture = _previewWebcam;
        previewRawImage.enabled = true;
        previewRawImage.gameObject.SetActive(true);
        (previewRawImage.transform as RectTransform)?.SetAsFirstSibling();
    }

    private bool EnsurePreviewSurface()
    {
        if (previewRawImage != null)
        {
            return true;
        }

        if (!autoCreatePreviewSurface)
        {
            Log("[CAM] preview RawImage is not assigned.");
            return false;
        }

        Canvas canvas = FindObjectOfType<Canvas>(includeInactive: true);
        if (canvas == null)
        {
            Log("[CAM] cannot auto-create preview: no Canvas found");
            return false;
        }

        GameObject go = new GameObject("PhotoPreview", typeof(RectTransform), typeof(RawImage));
        RectTransform rect = go.GetComponent<RectTransform>();
        rect.SetParent(canvas.transform, worldPositionStays: false);
        rect.anchorMin = Vector2.zero;
        rect.anchorMax = Vector2.one;
        rect.offsetMin = Vector2.zero;
        rect.offsetMax = Vector2.zero;
        rect.localScale = Vector3.one;

        previewRawImage = go.GetComponent<RawImage>();
        previewRawImage.color = Color.white;
        previewRawImage.raycastTarget = false;
        previewRawImage.enabled = false;
        go.SetActive(false);

        Log("[CAM] auto-created preview RawImage");
        return true;
    }

    private int ChooseDeviceIndex(WebCamDevice[] devices)
    {
        if (!preferRearCamera)
        {
            return 0;
        }

        for (int i = 0; i < devices.Length; i++)
        {
            if (!devices[i].isFrontFacing)
            {
                return i;
            }
        }

        return 0;
    }

    private async Task<bool> WaitForWebcamReadyAsync(WebCamTexture webcam)
    {
        float deadline = Time.realtimeSinceStartup + Mathf.Max(0.2f, warmupTimeoutSeconds);
        while (Time.realtimeSinceStartup < deadline)
        {
            await Task.Yield();
            if (webcam != null && webcam.didUpdateThisFrame && webcam.width > 16 && webcam.height > 16)
            {
                break;
            }
        }

        if (webcam == null || webcam.width <= 16 || webcam.height <= 16)
        {
            return false;
        }

        int settleFrames = Mathf.Max(0, warmupFrames);
        for (int i = 0; i < settleFrames; i++)
        {
            await Task.Yield();
        }

        return webcam.width > 16 && webcam.height > 16;
    }

    private static Texture2D ResizeToMaxDimension(Texture2D source, int maxDim, out int outWidth, out int outHeight)
    {
        if (source == null)
        {
            outWidth = 0;
            outHeight = 0;
            return null;
        }

        int srcWidth = Mathf.Max(1, source.width);
        int srcHeight = Mathf.Max(1, source.height);
        int longest = Mathf.Max(srcWidth, srcHeight);
        if (longest <= Mathf.Max(1, maxDim))
        {
            outWidth = srcWidth;
            outHeight = srcHeight;
            return source;
        }

        float scale = maxDim / (float)longest;
        outWidth = Mathf.Max(1, Mathf.RoundToInt(srcWidth * scale));
        outHeight = Mathf.Max(1, Mathf.RoundToInt(srcHeight * scale));

        RenderTexture rt = RenderTexture.GetTemporary(outWidth, outHeight, 0, RenderTextureFormat.ARGB32);
        RenderTexture prev = RenderTexture.active;
        Graphics.Blit(source, rt);
        RenderTexture.active = rt;

        Texture2D resized = new Texture2D(outWidth, outHeight, TextureFormat.RGB24, false);
        resized.ReadPixels(new Rect(0f, 0f, outWidth, outHeight), 0, 0);
        resized.Apply(updateMipmaps: false, makeNoLongerReadable: false);

        RenderTexture.active = prev;
        RenderTexture.ReleaseTemporary(rt);
        return resized;
    }

    private async Task<bool> EnsureCameraPermissionAsync()
    {
#if UNITY_ANDROID && !UNITY_EDITOR
        if (Permission.HasUserAuthorizedPermission(Permission.Camera))
        {
            Log("[CAM] permission already granted");
            return true;
        }

        Log("[CAM] requesting camera permission");
        Permission.RequestUserPermission(Permission.Camera);

        float deadline = Time.realtimeSinceStartup + 6f;
        while (Time.realtimeSinceStartup < deadline)
        {
            await Task.Yield();
            if (Permission.HasUserAuthorizedPermission(Permission.Camera))
            {
                Log("[CAM] permission granted");
                return true;
            }
        }

        bool granted = Permission.HasUserAuthorizedPermission(Permission.Camera);
        Log(granted ? "[CAM] permission granted" : "[CAM] permission denied by user");
        return granted;
#else
        await Task.Yield();
        return true;
#endif
    }

    private void Log(string message)
    {
        if (!debugLogs)
        {
            return;
        }

        Debug.Log(message);
    }

    private void ClearStillPreview()
    {
        if (_stillTexture == null)
        {
            return;
        }

        Destroy(_stillTexture);
        _stillTexture = null;
    }
}
