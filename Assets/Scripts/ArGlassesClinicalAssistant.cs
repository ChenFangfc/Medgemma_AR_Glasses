using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Threading.Tasks;
using UnityEngine;
#if UNITY_EDITOR
using UnityEditor;
#endif

public class ArGlassesClinicalAssistant : MonoBehaviour
{
    private const string DefaultWorkflowUrl = "ws://127.0.0.1:8003/ws";

    private enum ViewMode
    {
        Note,
        Advice
    }

    private enum DetailLevel
    {
        Short,
        Full
    }

    private enum ViewPage
    {
        NoteShort,
        AdviceShort,
        NoteFull,
        AdviceFull
    }

    private enum PhotoOverlayState
    {
        None,
        AddPrompt,
        CapturePrompt,
        ReviewPrompt,
        Busy
    }

    private struct PendingRequest
    {
        public string Op;
        public string What;
        public float StartedAt;
    }

    [Header("Core Components")]
    [SerializeField] private ArWorkflowWebSocket workflowSocket;
    [SerializeField] private ArAudioRecorder audioRecorder;
    [SerializeField] private ArWheelInputRouter inputRouter;
    [SerializeField] private ArGlassesUiPresenter uiPresenter;
    [SerializeField] private ArPhotoCapture photoCapture;

    [Header("Workflow Settings")]
    [SerializeField] private string workflowUrl = DefaultWorkflowUrl;
    [SerializeField] private string patientId = string.Empty;
    [SerializeField] private bool generatePatientIdPerLaunch = true;
    [SerializeField] private string patientIdPrefix = "p_argo";
    [SerializeField] private float requestTimeoutSeconds = 45f;
    [SerializeField] private bool allowSessionRecreateOnUnknownSession = false;

    [Header("Audio Upload Strategy")]
    [SerializeField] private bool enableChunkedUpload = true;
    [SerializeField] [Range(100, 2000)] private int chunkMs = 500;
    [SerializeField] private bool fallbackToSingleShotOnChunkFailure = true;
    [SerializeField] private bool logChunkTraffic = false;

    [Header("Photo Enhancement (Optional)")]
    [SerializeField] private bool enablePhotoEnhancement = true;
    [SerializeField] private bool promptForPhotoAfterTurnResult = true;
    [SerializeField] [Range(1f, 10f)] private float addPhotoPromptTimeoutSeconds = 3f;
    [SerializeField] [Range(256, 2048)] private int photoMaxDimension = 1024;
    [SerializeField] [Range(40, 100)] private int photoJpegQuality = 80;
    [SerializeField] private bool debugPhotoLogs = true;

    [Header("Input Behavior")]
    [SerializeField] private bool quitOnDoublePress = true;
    [SerializeField] private bool quitOnEscapeKey = true;
    [SerializeField] private bool sendEndSessionBeforeQuit = true;
    [SerializeField] [Range(0f, 3f)] private float quitWaitForEndSessionSeconds = 0.6f;

    [Header("Earcon Settings")]
    [SerializeField] private AudioSource earconSource;
    [SerializeField] private bool silentMode;
    [SerializeField] [Range(0f, 1f)] private float earconVolume = 0.35f;
    [SerializeField] private float startBeepFrequency = 1200f;
    [SerializeField] private float stopBeepFrequency = 760f;
    [SerializeField] private float beepDurationSeconds = 0.12f;
    [SerializeField] private float stopBeepGapSeconds = 0.07f;

    private readonly Dictionary<string, string> _cache = new Dictionary<string, string>(StringComparer.Ordinal);
    private readonly Dictionary<string, PendingRequest> _pendingRequests = new Dictionary<string, PendingRequest>(StringComparer.Ordinal);
    private readonly List<string> _timedOutRequestIds = new List<string>();

    private ViewMode _mode = ViewMode.Note;
    private DetailLevel _detail = DetailLevel.Full;
    private bool _recording;
    private bool _processing;
    private float _recordingStartedAt;
    private int _requestCounter;
    private string _sessionId = string.Empty;
    private int _turnIndex;
    private byte[] _lastBufferedAudio;
    private byte[] _pendingAudioAfterSessionStart;
    private AudioClip _startEarconClip;
    private AudioClip _stopEarconClip;
    private bool _warnedMissingUiPresenter;
    private string _activeTurnId = string.Empty;
    private int _nextChunkSeq = 1;
    private bool _audioBeginInFlight;
    private bool _streamingTurnBegun;
    private bool _chunkBeginFailed;
    private bool _launchPatientInitialized;
    private bool _quitInProgress;
    private string _pendingQuitRequestId = string.Empty;
    private Coroutine _quitDelayCoroutine;
    private byte[] _lastTurnAudioForPhoto;
    private byte[] _capturedPhotoBytes;
    private int _capturedPhotoWidth;
    private int _capturedPhotoHeight;
    private string _capturedPhotoMime = "image/jpeg";
    private bool _photoUploadInFlight;
    private bool _capturePhotoInFlight;
    private PhotoOverlayState _photoOverlayState = PhotoOverlayState.None;
    private int _photoOverlaySelection;
    private float _photoAddPromptDeadline = -1f;

    private void Awake()
    {
        if (workflowSocket == null)
        {
            workflowSocket = GetComponent<ArWorkflowWebSocket>();
        }

        if (audioRecorder == null)
        {
            audioRecorder = GetComponent<ArAudioRecorder>();
        }

        if (inputRouter == null)
        {
            inputRouter = GetComponent<ArWheelInputRouter>();
        }

        if (uiPresenter == null)
        {
            uiPresenter = GetComponent<ArGlassesUiPresenter>();
        }

        if (uiPresenter == null)
        {
            // UI presenter usually lives on a separate UI root object in scene.
            uiPresenter = FindObjectOfType<ArGlassesUiPresenter>(includeInactive: true);
        }

        if (photoCapture == null)
        {
            photoCapture = GetComponent<ArPhotoCapture>();
        }

        if (photoCapture == null)
        {
            photoCapture = gameObject.AddComponent<ArPhotoCapture>();
            if (debugPhotoLogs)
            {
                Debug.Log("[ArGlassesClinicalAssistant] Added ArPhotoCapture component at runtime.");
            }
        }

        if (earconSource == null)
        {
            earconSource = GetComponent<AudioSource>();
        }

        EnsureLaunchPatientIdentity();
        BuildEarcons();
        InitializeUi();
    }

    private void OnEnable()
    {
        EnsureLaunchPatientIdentity();
        BindEvents();
        if (workflowSocket != null)
        {
            string resolvedUrl = ResolveWorkflowUrl();
            workflowSocket.ServerUrl = resolvedUrl;

            if (IsLoopbackWorkflowUrl(resolvedUrl))
            {
                Debug.LogWarning(
                    $"Workflow URL is set to loopback ({resolvedUrl}). On ARGO device this points to the glasses itself. " +
                    "Set a LAN server URL like ws://<server-ip>:8003/ws, or use adb reverse for local USB tunneling.");
                uiPresenter?.SetStatus("Config: set Workflow Url to ws://<server-ip>:8003/ws");
            }
            else if (IsInsecureNgrokUrl(resolvedUrl))
            {
                Debug.LogWarning(
                    $"Workflow URL uses ws:// with ngrok ({resolvedUrl}). Prefer wss://<subdomain>.ngrok-free.dev/ws.");
            }

            workflowSocket.Connect();
        }
    }

    private void OnDisable()
    {
        UnbindEvents();
        photoCapture?.StopPreview();
    }

    private void OnDestroy()
    {
        if (_startEarconClip != null)
        {
            Destroy(_startEarconClip);
        }

        if (_stopEarconClip != null)
        {
            Destroy(_stopEarconClip);
        }
    }

    private void OnApplicationQuit()
    {
        // Best-effort only; app may terminate before response arrives.
        if (!_quitInProgress
            && sendEndSessionBeforeQuit
            && !string.IsNullOrEmpty(_sessionId)
            && workflowSocket != null
            && workflowSocket.IsConnected)
        {
            EndSession(includeTranscript: true);
        }
    }

    private void Update()
    {
        // On ARGO devices wheel double-click is often delivered as Escape.
        if (!_quitInProgress && quitOnDoublePress && quitOnEscapeKey && Input.GetKeyDown(KeyCode.Escape))
        {
            Debug.Log("[ArGlassesClinicalAssistant] ESCAPE received -> QuitApplication()");
            QuitApplication();
            return;
        }

        if (_recording && uiPresenter != null)
        {
            uiPresenter.SetRecordingState(true, Time.unscaledTime - _recordingStartedAt);
        }

        if (_recording && enableChunkedUpload)
        {
            TrySendAudioBeginIfReady();
            PumpChunkStreaming(flush: false);
        }

        UpdatePhotoOverlayTimeout();
        CheckRequestTimeouts();
    }

    public void RetryLastUpload()
    {
        if (_quitInProgress)
        {
            return;
        }

        if (IsPhotoOverlayActive())
        {
            uiPresenter?.SetStatus("Close photo dialog first");
            return;
        }

        if (_lastBufferedAudio == null || _lastBufferedAudio.Length == 0)
        {
            uiPresenter?.SetStatus("No buffered audio to retry.");
            return;
        }

        if (_recording)
        {
            return;
        }

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            _processing = false;
            uiPresenter?.SetStatus("Upload failed - retry");
            return;
        }

        _processing = true;
        uiPresenter?.SetStatus("Processing...");

        if (string.IsNullOrEmpty(_sessionId))
        {
            _pendingAudioAfterSessionStart = _lastBufferedAudio;
            RequestStartSession();
            return;
        }

        SendProcessAudio(_lastBufferedAudio);
    }

    public void ToggleRecording()
    {
        HandleLongPress();
    }

    public void StartRecordingIfIdle()
    {
        if (_recording)
        {
            return;
        }

        StartRecording();
    }

    public void StopRecordingIfActive()
    {
        if (!_recording)
        {
            return;
        }

        StopRecordingAndProcess();
    }

    public void NextViewPage()
    {
        ViewPage before = CurrentViewPage();
        ViewPage after = GetNextViewPage(before);
        SetViewPage(after);
        Debug.Log($"[ArGlassesClinicalAssistant] NextViewPage() {ViewPageLabel(before)} -> {ViewPageLabel(after)}");
        ApplyCurrentView();
    }

    public void GoHomeView()
    {
        ViewPage before = CurrentViewPage();
        SetViewPage(ViewPage.NoteFull);
        Debug.Log($"[ArGlassesClinicalAssistant] GoHomeView() {ViewPageLabel(before)} -> NOTE_FULL");
        ApplyCurrentView();
    }

    public void ToggleDetailLevel()
    {
        _detail = DetailLevel.Full;
        ApplyCurrentView();
    }

    public void ToggleMode()
    {
        _mode = _mode == ViewMode.Note ? ViewMode.Advice : ViewMode.Note;
        _detail = DetailLevel.Full;
        ApplyCurrentView();
    }

    public void ShowNote()
    {
        _mode = ViewMode.Note;
        _detail = DetailLevel.Full;
        ApplyCurrentView();
    }

    public void ShowAdvice()
    {
        _mode = ViewMode.Advice;
        _detail = DetailLevel.Full;
        ApplyCurrentView();
    }

    public void ShowShort()
    {
        // Short views are deprecated in the new server contract.
        ShowFull();
    }

    public void ShowFull()
    {
        _detail = DetailLevel.Full;
        ApplyCurrentView();
    }

    public void EndSession(bool includeTranscript = true)
    {
        if (string.IsNullOrEmpty(_sessionId))
        {
            uiPresenter?.SetStatus("No active session.");
            return;
        }

        string requestId = NextRequestId("e");
        Dictionary<string, object> payload = new Dictionary<string, object>
        {
            { "op", "end_session" },
            { "request_id", requestId },
            { "session_id", _sessionId },
            { "include_transcript", includeTranscript },
            { "close_session", true }
        };
        SendRequest(payload, requestId, "end_session", string.Empty);
    }

    private void BindEvents()
    {
        if (workflowSocket != null)
        {
            workflowSocket.Connected += HandleSocketConnected;
            workflowSocket.Disconnected += HandleSocketDisconnected;
            workflowSocket.TextMessageReceived += HandleSocketMessage;
            workflowSocket.Error += HandleSocketError;
        }

        if (inputRouter != null)
        {
            inputRouter.SinglePress += HandleSinglePress;
            inputRouter.DoublePress += HandleDoublePress;
            inputRouter.LongPress += HandleLongPress;
            inputRouter.Scroll += HandleScroll;
        }
    }

    private void UnbindEvents()
    {
        if (workflowSocket != null)
        {
            workflowSocket.Connected -= HandleSocketConnected;
            workflowSocket.Disconnected -= HandleSocketDisconnected;
            workflowSocket.TextMessageReceived -= HandleSocketMessage;
            workflowSocket.Error -= HandleSocketError;
        }

        if (inputRouter != null)
        {
            inputRouter.SinglePress -= HandleSinglePress;
            inputRouter.DoublePress -= HandleDoublePress;
            inputRouter.LongPress -= HandleLongPress;
            inputRouter.Scroll -= HandleScroll;
        }
    }

    private void InitializeUi()
    {
        if (uiPresenter == null)
        {
            WarnMissingUiPresenter("InitializeUi");
            return;
        }

        uiPresenter.SetHeader(CurrentHeader());
        uiPresenter.SetConnectivity(false);
        uiPresenter.SetStatus("Connecting...");
        uiPresenter.SetRecordingState(false, 0f);
        uiPresenter.SetContent("No result yet.\nLong press to start recording.", scrollable: false);
    }

    private void EnsureLaunchPatientIdentity()
    {
        if (_launchPatientInitialized)
        {
            return;
        }

        _launchPatientInitialized = true;
        if (generatePatientIdPerLaunch)
        {
            patientId = BuildLaunchPatientId();
        }

        ResetLocalStateForNewPatient();
        Debug.Log($"[ArGlassesClinicalAssistant] Launch patient initialized: patient_id={patientId}");
    }

    private void ResetLocalStateForNewPatient()
    {
        _sessionId = string.Empty;
        _turnIndex = 0;
        _cache.Clear();
        _pendingRequests.Clear();
        _pendingAudioAfterSessionStart = null;
        _lastBufferedAudio = null;
        _lastTurnAudioForPhoto = null;
        _capturedPhotoBytes = null;
        _capturedPhotoWidth = 0;
        _capturedPhotoHeight = 0;
        _capturedPhotoMime = "image/jpeg";
        _photoUploadInFlight = false;
        _capturePhotoInFlight = false;
        _photoOverlayState = PhotoOverlayState.None;
        _photoOverlaySelection = 0;
        _photoAddPromptDeadline = -1f;
        _processing = false;
        _mode = ViewMode.Note;
        _detail = DetailLevel.Full;
        ResetChunkedTurnState();
    }

    private string BuildLaunchPatientId()
    {
        string prefix = SanitizeIdPart(string.IsNullOrWhiteSpace(patientIdPrefix) ? "p_argo" : patientIdPrefix);
        string stamp = DateTimeOffset.UtcNow.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture);
        string suffix = Guid.NewGuid().ToString("N").Substring(0, 8);
        return $"{prefix}_{stamp}_{suffix}";
    }

    private static string SanitizeIdPart(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
        {
            return "p_argo";
        }

        StringBuilder builder = new StringBuilder(raw.Length);
        for (int i = 0; i < raw.Length; i++)
        {
            char c = raw[i];
            if (char.IsLetterOrDigit(c) || c == '_' || c == '-')
            {
                builder.Append(c);
            }
        }

        return builder.Length == 0 ? "p_argo" : builder.ToString();
    }

    private string ResolveWorkflowUrl()
    {
        string assistantUrl = (workflowUrl ?? string.Empty).Trim();
        string socketUrl = workflowSocket != null ? (workflowSocket.ServerUrl ?? string.Empty).Trim() : string.Empty;

        bool assistantIsDefault = string.Equals(assistantUrl, DefaultWorkflowUrl, StringComparison.OrdinalIgnoreCase);
        bool socketIsDefault = string.Equals(socketUrl, DefaultWorkflowUrl, StringComparison.OrdinalIgnoreCase);

        string resolved;
        if (!string.IsNullOrWhiteSpace(assistantUrl) && (!assistantIsDefault || string.IsNullOrWhiteSpace(socketUrl) || socketIsDefault))
        {
            resolved = assistantUrl;
        }
        else if (!string.IsNullOrWhiteSpace(socketUrl))
        {
            resolved = socketUrl;
        }
        else
        {
            resolved = assistantUrl;
        }

        resolved = NormalizeWorkflowUrl(resolved);
        workflowUrl = resolved;
        Debug.Log($"Workflow URL resolved to: {resolved}");
        return resolved;
    }

    private static string NormalizeWorkflowUrl(string rawUrl)
    {
        if (string.IsNullOrWhiteSpace(rawUrl))
        {
            return rawUrl;
        }

        string normalized = rawUrl.Trim();
        if (normalized.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
        {
            normalized = "wss://" + normalized.Substring("https://".Length);
        }
        else if (normalized.StartsWith("http://", StringComparison.OrdinalIgnoreCase))
        {
            normalized = "ws://" + normalized.Substring("http://".Length);
        }

        if (!Uri.TryCreate(normalized, UriKind.Absolute, out Uri uri))
        {
            return normalized;
        }

        string scheme = uri.Scheme;
        bool isWsScheme =
            string.Equals(scheme, "ws", StringComparison.OrdinalIgnoreCase)
            || string.Equals(scheme, "wss", StringComparison.OrdinalIgnoreCase);
        if (!isWsScheme)
        {
            return normalized;
        }

        // Default workflow server path is /ws.
        if (string.IsNullOrEmpty(uri.AbsolutePath) || string.Equals(uri.AbsolutePath, "/", StringComparison.Ordinal))
        {
            UriBuilder builder = new UriBuilder(uri)
            {
                Path = "/ws"
            };
            return builder.Uri.AbsoluteUri;
        }

        return normalized;
    }

    private static bool IsLoopbackWorkflowUrl(string url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return false;
        }

        if (!Uri.TryCreate(url, UriKind.Absolute, out Uri uri))
        {
            return false;
        }

        string host = uri.Host;
        if (string.IsNullOrWhiteSpace(host))
        {
            return false;
        }

        if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        return string.Equals(host, "127.0.0.1", StringComparison.OrdinalIgnoreCase)
            || string.Equals(host, "0.0.0.0", StringComparison.OrdinalIgnoreCase)
            || string.Equals(host, "::1", StringComparison.OrdinalIgnoreCase);
    }

    private static bool IsInsecureNgrokUrl(string url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return false;
        }

        if (!Uri.TryCreate(url, UriKind.Absolute, out Uri uri))
        {
            return false;
        }

        bool isNgrokHost = uri.Host.IndexOf("ngrok", StringComparison.OrdinalIgnoreCase) >= 0;
        bool isWs = string.Equals(uri.Scheme, "ws", StringComparison.OrdinalIgnoreCase);
        return isNgrokHost && isWs;
    }

    private void HandleSocketConnected()
    {
        if (_quitInProgress)
        {
            PerformQuitNow();
            return;
        }

        uiPresenter?.SetConnectivity(true);
        uiPresenter?.SetStatus("Online");

        if (string.IsNullOrEmpty(_sessionId))
        {
            RequestStartSession();
            return;
        }

        Debug.Log($"[ArGlassesClinicalAssistant] Socket reconnected. Reusing existing session_id={_sessionId}");

        if (_pendingAudioAfterSessionStart != null && _pendingAudioAfterSessionStart.Length > 0)
        {
            byte[] queuedAudio = _pendingAudioAfterSessionStart;
            _pendingAudioAfterSessionStart = null;
            SendProcessAudio(queuedAudio);
        }

        if (enableChunkedUpload && _recording)
        {
            TrySendAudioBeginIfReady();
        }
    }

    private void HandleSocketDisconnected()
    {
        // Requests in-flight before disconnect will not receive replies.
        _pendingRequests.Clear();
        _processing = false;

        if (_photoUploadInFlight)
        {
            _photoUploadInFlight = false;
            _capturePhotoInFlight = false;
            if (_photoOverlayState == PhotoOverlayState.Busy && _capturedPhotoBytes != null && _capturedPhotoBytes.Length > 0)
            {
                OpenPhotoReviewPrompt();
            }
        }

        if (enableChunkedUpload && _recording)
        {
            // Connection drop invalidates turn stream state; restart from the beginning on reconnect.
            _audioBeginInFlight = false;
            _streamingTurnBegun = false;
            _chunkBeginFailed = false;
            _activeTurnId = BuildTurnId();
            _nextChunkSeq = 1;
            audioRecorder?.ResetPcmReadCursor();
        }

        if (_quitInProgress)
        {
            PerformQuitNow();
            return;
        }

        uiPresenter?.SetConnectivity(false);
        uiPresenter?.SetStatus("Offline - retrying");
    }

    private void HandleSocketError(string error)
    {
        Debug.LogWarning(error);
    }

    private void HandleSocketMessage(string rawJson)
    {
        object parsed = ArSimpleJson.Deserialize(rawJson);
        if (!(parsed is Dictionary<string, object> message))
        {
            Debug.LogWarning($"Unexpected WS payload: {rawJson}");
            return;
        }

        string op = GetString(message, "op");
        if (string.IsNullOrEmpty(op))
        {
            return;
        }

        PendingRequest? requestContext = null;
        string requestId = GetString(message, "request_id");
        if (!string.IsNullOrEmpty(requestId))
        {
            if (_pendingRequests.TryGetValue(requestId, out PendingRequest pending))
            {
                requestContext = pending;
            }

            _pendingRequests.Remove(requestId);
        }

        Debug.Log($"[ArGlassesClinicalAssistant] WS recv op={op} request_id={requestId}");

        switch (op)
        {
            case "ready":
                // Informational banner; ignore per requirements.
                break;
            case "session_started":
                HandleSessionStarted(message);
                break;
            case "turn_result":
                HandleTurnResult(message);
                break;
            case "audio_ack":
                HandleAudioAck(message);
                break;
            case "chunk_ack":
                HandleChunkAck(message);
                break;
            case "get_result":
                HandleGetResult(message);
                break;
            case "session_summary":
                if (GetBool(message, "session_closed", fallback: false))
                {
                    InvalidateSession("server closed session");
                }
                uiPresenter?.SetStatus("Session ended.");
                if (_quitInProgress
                    && (string.IsNullOrEmpty(_pendingQuitRequestId)
                        || string.Equals(requestId, _pendingQuitRequestId, StringComparison.Ordinal)))
                {
                    PerformQuitNow();
                    return;
                }
                break;
            case "error":
                HandleServerError(message, requestContext);
                break;
            default:
                Debug.Log($"Unhandled op: {op}");
                break;
        }
    }

    private void HandleSessionStarted(Dictionary<string, object> message)
    {
        if (_quitInProgress)
        {
            PerformQuitNow();
            return;
        }

        string startedSessionId = GetString(message, "session_id");
        if (!string.IsNullOrWhiteSpace(startedSessionId))
        {
            _sessionId = startedSessionId;
        }
        _turnIndex = GetInt(message, "turn_count", _turnIndex);
        string serverPatientId = GetString(message, "patient_id");
        if (!string.IsNullOrWhiteSpace(serverPatientId))
        {
            patientId = serverPatientId;
        }

        uiPresenter?.SetStatus("Session ready.");

        if (_pendingAudioAfterSessionStart != null && _pendingAudioAfterSessionStart.Length > 0)
        {
            byte[] queuedAudio = _pendingAudioAfterSessionStart;
            _pendingAudioAfterSessionStart = null;
            SendProcessAudio(queuedAudio);
        }

        if (enableChunkedUpload && _recording)
        {
            TrySendAudioBeginIfReady();
        }
    }

    private void HandleTurnResult(Dictionary<string, object> message)
    {
        if (_quitInProgress)
        {
            PerformQuitNow();
            return;
        }

        bool photoUploadTurn = _photoUploadInFlight;
        _photoUploadInFlight = false;
        _capturePhotoInFlight = false;

        _processing = false;
        string turnSessionId = GetString(message, "session_id");
        if (!string.IsNullOrWhiteSpace(turnSessionId))
        {
            _sessionId = turnSessionId;
        }
        _turnIndex = GetInt(message, "turn_index", _turnIndex + 1);
        _cache.Clear();

        TryCacheValue(message, "note_full");
        TryCacheValue(message, "advice_full");
        TryCacheValue(message, "summary_turn");
        TryCacheValue(message, "running_summary");

        // Backward-compat fallback in case server still emits short aliases.
        if (!_cache.ContainsKey("note_full"))
        {
            TryCacheValue(message, "note_short");
            if (_cache.TryGetValue("note_short", out string noteShortAlias))
            {
                _cache["note_full"] = noteShortAlias;
            }
        }

        if (!_cache.ContainsKey("advice_full"))
        {
            TryCacheValue(message, "advice_short");
            if (_cache.TryGetValue("advice_short", out string adviceShortAlias))
            {
                _cache["advice_full"] = adviceShortAlias;
            }
        }

        _mode = ViewMode.Note;
        _detail = DetailLevel.Full;
        _lastBufferedAudio = null;
        ResetChunkedTurnState();

        if (_cache.TryGetValue("note_full", out string noteFull))
        {
            Debug.Log($"[ArGlassesClinicalAssistant] turn_result received turn={_turnIndex} note_full_len={noteFull.Length}");
        }
        else
        {
            Debug.LogWarning($"[ArGlassesClinicalAssistant] turn_result received turn={_turnIndex} but note_full missing");
        }

        if (_cache.TryGetValue("running_summary", out string runningSummary))
        {
            Debug.Log($"[ArGlassesClinicalAssistant] running_summary_len={runningSummary.Length}");
        }

        ApplyCurrentView();
        uiPresenter?.SetStatus("Ready");

        if (photoUploadTurn)
        {
            ClosePhotoOverlay(keepCapturedPhoto: true, restoreCurrentView: false, status: "Photo enhanced");
            return;
        }

        TryOpenAddPhotoPromptAfterTurn();
    }

    private void HandleGetResult(Dictionary<string, object> message)
    {
        if (_quitInProgress)
        {
            return;
        }

        string what = GetString(message, "what");
        if (string.IsNullOrEmpty(what))
        {
            return;
        }

        if (string.Equals(what, "note_short", StringComparison.Ordinal))
        {
            what = "note_full";
        }
        else if (string.Equals(what, "advice_short", StringComparison.Ordinal))
        {
            what = "advice_full";
        }

        string content = string.Empty;
        if (message.TryGetValue("text", out object textValue))
        {
            content = NormalizeText(textValue as string);
        }
        else if (message.TryGetValue("data", out object dataValue))
        {
            content = FormatObjectForDisplay(dataValue);
        }

        if (string.IsNullOrWhiteSpace(content))
        {
            content = "(empty)";
        }

        _cache[what] = content;

        if (string.Equals(CurrentViewKey(), what, StringComparison.Ordinal))
        {
            ApplyCurrentView();
        }

        uiPresenter?.SetStatus("Ready");
    }

    private void HandleServerError(Dictionary<string, object> message, PendingRequest? requestContext)
    {
        string detail = GetString(message, "message");
        if (string.IsNullOrWhiteSpace(detail))
        {
            detail = GetString(message, "detail");
        }

        string requestId = GetString(message, "request_id");
        string failedOp = requestContext.HasValue ? requestContext.Value.Op : InferOpFromRequestId(requestId);

        if (_quitInProgress)
        {
            if (!string.IsNullOrWhiteSpace(detail))
            {
                Debug.LogWarning($"[ArGlassesClinicalAssistant] error while quitting (op={failedOp}): {detail}");
            }

            PerformQuitNow();
            return;
        }

        if (TryRecoverFromSessionError(detail, requestContext))
        {
            return;
        }

        if (_photoUploadInFlight && string.Equals(failedOp, "process_audio", StringComparison.Ordinal))
        {
            _photoUploadInFlight = false;
            _capturePhotoInFlight = false;
            _processing = false;
            uiPresenter?.SetStatus("Photo upload failed");
            if (_photoOverlayState == PhotoOverlayState.Busy)
            {
                OpenPhotoReviewPrompt();
            }

            if (!string.IsNullOrWhiteSpace(detail))
            {
                Debug.LogWarning($"[ArGlassesClinicalAssistant] Photo upload error: {detail}");
            }

            return;
        }

        if (string.Equals(failedOp, "audio_begin", StringComparison.Ordinal)
            || string.Equals(failedOp, "audio_chunk", StringComparison.Ordinal))
        {
            _audioBeginInFlight = false;
            _streamingTurnBegun = false;
            _chunkBeginFailed = true;
            if (_recording)
            {
                uiPresenter?.SetStatus("Recording...");
            }

            if (!string.IsNullOrWhiteSpace(detail))
            {
                Debug.LogWarning($"[ArGlassesClinicalAssistant] {failedOp} error: {detail}");
            }

            return;
        }

        if (string.Equals(failedOp, "audio_end", StringComparison.Ordinal)
            && fallbackToSingleShotOnChunkFailure
            && _lastBufferedAudio != null
            && _lastBufferedAudio.Length > 0
            && !_recording)
        {
            Debug.LogWarning("[ArGlassesClinicalAssistant] audio_end failed, falling back to process_audio.");
            SendProcessAudio(_lastBufferedAudio);
            return;
        }

        _processing = false;
        if (_recording)
        {
            // While recording, "processing failed" is misleading.
            uiPresenter?.SetStatus("Recording...");
        }
        else
        {
            uiPresenter?.SetStatus("Processing failed");
        }

        if (!string.IsNullOrWhiteSpace(detail))
        {
            Debug.LogWarning($"Server error op={failedOp} request_id={requestId}: {detail}");
        }
    }

    private bool TryRecoverFromSessionError(string detail, PendingRequest? requestContext)
    {
        if (!IsUnknownSessionError(detail))
        {
            return false;
        }

        string failedOp = requestContext.HasValue ? requestContext.Value.Op : "unknown";

        if (!allowSessionRecreateOnUnknownSession)
        {
            Debug.LogError(
                $"[ArGlassesClinicalAssistant] Unknown/expired session on op={failedOp}. " +
                "Auto recreate is disabled to keep one session per app launch. Please restart the app for a new patient/session.");

            _processing = false;
            _audioBeginInFlight = false;
            _streamingTurnBegun = false;
            _chunkBeginFailed = true;
            _pendingAudioAfterSessionStart = null;
            _photoUploadInFlight = false;
            _capturePhotoInFlight = false;

            if (_recording && audioRecorder != null)
            {
                audioRecorder.StopRecordingAsWav(out _);
                _recording = false;
                uiPresenter?.SetRecordingState(false, 0f);
            }

            uiPresenter?.SetStatus("Session lost - restart app");
            return true;
        }

        Debug.LogWarning($"[ArGlassesClinicalAssistant] Unknown/expired session on op={failedOp}. Recreating session.");
        InvalidateSession(detail);

        if (enableChunkedUpload && _recording)
        {
            _audioBeginInFlight = false;
            _streamingTurnBegun = false;
            _chunkBeginFailed = false;
            _activeTurnId = BuildTurnId();
            _nextChunkSeq = 1;
            audioRecorder?.ResetPcmReadCursor();

            _processing = false;
            uiPresenter?.SetStatus("Session expired - recreating...");
            RequestStartSession();
            return true;
        }

        if (requestContext.HasValue && string.Equals(requestContext.Value.Op, "process_audio", StringComparison.Ordinal))
        {
            if (_lastBufferedAudio != null && _lastBufferedAudio.Length > 0)
            {
                _pendingAudioAfterSessionStart = _lastBufferedAudio;
                _processing = true;
                uiPresenter?.SetStatus("Session expired - recreating...");
                RequestStartSession();
                return true;
            }
        }

        if (requestContext.HasValue && string.Equals(requestContext.Value.Op, "audio_end", StringComparison.Ordinal))
        {
            if (_lastBufferedAudio != null && _lastBufferedAudio.Length > 0)
            {
                _pendingAudioAfterSessionStart = _lastBufferedAudio;
                _processing = true;
                uiPresenter?.SetStatus("Session expired - recreating...");
                RequestStartSession();
                return true;
            }
        }

        _processing = false;
        uiPresenter?.SetStatus("Session expired - recreating...");
        RequestStartSession();
        return true;
    }

    private void HandleSinglePress()
    {
        if (_quitInProgress)
        {
            return;
        }

        if (IsPhotoOverlayActive())
        {
            HandlePhotoOverlaySinglePress();
            return;
        }

        Debug.Log("[ArGlassesClinicalAssistant] Single press received.");
        NextViewPage();
    }

    private void HandleDoublePress()
    {
        if (_quitInProgress)
        {
            return;
        }

        Debug.Log("[ArGlassesClinicalAssistant] Double press received.");
        if (quitOnDoublePress)
        {
            QuitApplication();
            return;
        }

        GoHomeView();
    }

    public void QuitApplication()
    {
        if (_quitInProgress)
        {
            return;
        }

        _quitInProgress = true;
        Debug.Log("[ArGlassesClinicalAssistant] QuitApplication requested.");
        uiPresenter?.SetStatus("Exiting...");
        photoCapture?.StopPreview();

        if (_recording && audioRecorder != null)
        {
            audioRecorder.StopRecordingAsWav(out _);
            _recording = false;
            uiPresenter?.SetRecordingState(false, 0f);
        }

        if (sendEndSessionBeforeQuit
            && workflowSocket != null
            && workflowSocket.IsConnected
            && !string.IsNullOrEmpty(_sessionId))
        {
            string requestId = NextRequestId("eq");
            _pendingQuitRequestId = requestId;

            Dictionary<string, object> payload = new Dictionary<string, object>
            {
                { "op", "end_session" },
                { "request_id", requestId },
                { "session_id", _sessionId },
                { "include_transcript", true },
                { "close_session", true }
            };

            SendRequest(payload, requestId, "end_session_quit", string.Empty);
            _quitDelayCoroutine = StartCoroutine(QuitAfterDelayRealtime(quitWaitForEndSessionSeconds));
            return;
        }

        PerformQuitNow();
    }

    private IEnumerator QuitAfterDelayRealtime(float seconds)
    {
        float wait = Mathf.Max(0f, seconds);
        if (wait > 0f)
        {
            yield return new WaitForSecondsRealtime(wait);
        }

        if (_quitInProgress)
        {
            PerformQuitNow();
        }
    }

    private void PerformQuitNow()
    {
        if (_quitDelayCoroutine != null)
        {
            StopCoroutine(_quitDelayCoroutine);
            _quitDelayCoroutine = null;
        }

        _pendingQuitRequestId = string.Empty;

#if UNITY_EDITOR
        EditorApplication.isPlaying = false;
#else
        Application.Quit();
#endif
    }

    private void HandleLongPress()
    {
        if (_quitInProgress)
        {
            return;
        }

        if (IsPhotoOverlayActive())
        {
            uiPresenter?.SetStatus("Photo dialog open");
            return;
        }

        Debug.Log($"[ArGlassesClinicalAssistant] Long press received. Recording before={_recording}");
        if (_recording)
        {
            StopRecordingAndProcess();
        }
        else
        {
            StartRecording();
        }
    }

    private void HandleScroll(float delta)
    {
        if (_quitInProgress)
        {
            return;
        }

        if (IsPhotoOverlayActive())
        {
            HandlePhotoOverlayScroll(delta);
            return;
        }

        Debug.Log($"[ArGlassesClinicalAssistant] Scroll received. Delta={delta:0.###}");
        uiPresenter?.Scroll(delta);
    }

    public async void DebugCapturePhotoOnce()
    {
        if (photoCapture == null)
        {
            photoCapture = GetComponent<ArPhotoCapture>();
        }

        if (photoCapture == null)
        {
            Debug.LogWarning("[ArGlassesClinicalAssistant] DebugCapturePhotoOnce skipped: photoCapture missing.");
            return;
        }

        byte[] bytes = await photoCapture.CaptureJpegAsync(photoMaxDimension, photoJpegQuality);
        if (bytes == null || bytes.Length == 0)
        {
            uiPresenter?.SetStatus("Debug photo failed");
            return;
        }

        uiPresenter?.SetStatus($"Debug photo ok ({bytes.Length / 1024f:0.0}KB)");
    }

    private bool IsPhotoOverlayActive()
    {
        return _photoOverlayState != PhotoOverlayState.None;
    }

    private void UpdatePhotoOverlayTimeout()
    {
        if (_photoOverlayState != PhotoOverlayState.AddPrompt)
        {
            return;
        }

        if (_photoAddPromptDeadline <= 0f || Time.unscaledTime < _photoAddPromptDeadline)
        {
            return;
        }

        if (debugPhotoLogs)
        {
            Debug.Log("[ArGlassesClinicalAssistant] Photo add prompt timeout -> default No.");
        }

        ClosePhotoOverlay(keepCapturedPhoto: false, restoreCurrentView: true, status: "Photo skipped");
    }

    private void TryOpenAddPhotoPromptAfterTurn()
    {
        if (!enablePhotoEnhancement || !promptForPhotoAfterTurnResult)
        {
            return;
        }

        if (_lastTurnAudioForPhoto == null || _lastTurnAudioForPhoto.Length == 0)
        {
            return;
        }

        if (IsPhotoOverlayActive())
        {
            return;
        }

        if (_recording || _processing || _quitInProgress)
        {
            return;
        }

        OpenAddPhotoPrompt();
    }

    private void OpenAddPhotoPrompt()
    {
        _photoAddPromptDeadline = Time.unscaledTime + Mathf.Max(1f, addPhotoPromptTimeoutSeconds);
        _photoOverlayState = PhotoOverlayState.AddPrompt;
        _photoOverlaySelection = 0;
        photoCapture?.StopPreview();
        RenderPhotoOverlay();
    }

    private void OpenCapturePrompt()
    {
        _photoAddPromptDeadline = -1f;
        _photoOverlayState = PhotoOverlayState.CapturePrompt;
        _photoOverlaySelection = 0;
        RenderPhotoOverlay();
        StartPhotoPreviewAsync();
    }

    private void OpenPhotoReviewPrompt()
    {
        _photoAddPromptDeadline = -1f;
        _photoOverlayState = PhotoOverlayState.ReviewPrompt;
        _photoOverlaySelection = 0;
        bool showingCaptured = false;
        if (photoCapture != null && _capturedPhotoBytes != null && _capturedPhotoBytes.Length > 0)
        {
            showingCaptured = photoCapture.ShowCapturedImage(_capturedPhotoBytes);
        }

        if (!showingCaptured)
        {
            photoCapture?.StopPreview();
        }

        RenderPhotoOverlay();
    }

    private void RenderPhotoOverlay()
    {
        if (uiPresenter == null)
        {
            return;
        }

        uiPresenter.SetHeader("PHOTO");

        string content;
        bool scrollable = false;
        switch (_photoOverlayState)
        {
            case PhotoOverlayState.AddPrompt:
            {
                string[] options = { "Yes", "No", "Back" };
                content = "Add photo to refine this turn?\n\n" + FormatOverlayOptions(options);
                uiPresenter.SetStatus("Photo option");
                break;
            }
            case PhotoOverlayState.CapturePrompt:
            {
                string[] options = { "Capture", "Back" };
                content = "Press capture to take a photo.\n\n" + FormatOverlayOptions(options);
                uiPresenter.SetStatus("Photo capture");
                break;
            }
            case PhotoOverlayState.ReviewPrompt:
            {
                string[] options = { "Retake", "Upload", "Back" };
                string sizeText = _capturedPhotoBytes == null
                    ? "No photo captured yet."
                    : $"Captured: {_capturedPhotoWidth}x{_capturedPhotoHeight}, {_capturedPhotoBytes.Length / 1024f:0.0}KB";
                content = sizeText + "\n\n" + FormatOverlayOptions(options);
                uiPresenter.SetStatus("Photo review");
                break;
            }
            case PhotoOverlayState.Busy:
            default:
                content = "Working with photo...\nPlease wait.";
                uiPresenter.SetStatus("Photo processing...");
                break;
        }

        uiPresenter.SetContent(content, scrollable);
    }

    private string FormatOverlayOptions(IReadOnlyList<string> options)
    {
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < options.Count; i++)
        {
            if (i > 0)
            {
                builder.Append('\n');
            }

            bool selected = i == _photoOverlaySelection;
            builder.Append(selected ? "> " : "  ");
            builder.Append(options[i]);
        }

        return builder.ToString();
    }

    private void HandlePhotoOverlayScroll(float delta)
    {
        if (_photoOverlayState == PhotoOverlayState.Busy || Mathf.Abs(delta) < Mathf.Epsilon)
        {
            return;
        }

        int optionCount;
        switch (_photoOverlayState)
        {
            case PhotoOverlayState.AddPrompt:
                optionCount = 3;
                break;
            case PhotoOverlayState.CapturePrompt:
                optionCount = 2;
                break;
            case PhotoOverlayState.ReviewPrompt:
                optionCount = 3;
                break;
            default:
                return;
        }

        int step = delta < 0f ? 1 : -1;
        _photoOverlaySelection = (_photoOverlaySelection + step + optionCount) % optionCount;
        RenderPhotoOverlay();
    }

    private void HandlePhotoOverlaySinglePress()
    {
        if (_photoOverlayState == PhotoOverlayState.Busy)
        {
            return;
        }

        switch (_photoOverlayState)
        {
            case PhotoOverlayState.AddPrompt:
                if (_photoOverlaySelection == 0)
                {
                    OpenCapturePrompt();
                }
                else
                {
                    ClosePhotoOverlay(keepCapturedPhoto: false, restoreCurrentView: true, status: "Ready");
                }

                break;
            case PhotoOverlayState.CapturePrompt:
                if (_photoOverlaySelection == 0)
                {
                    BeginCapturePhotoAsync();
                }
                else
                {
                    ClosePhotoOverlay(keepCapturedPhoto: false, restoreCurrentView: true, status: "Ready");
                }

                break;
            case PhotoOverlayState.ReviewPrompt:
                if (_photoOverlaySelection == 0)
                {
                    OpenCapturePrompt();
                }
                else if (_photoOverlaySelection == 1)
                {
                    UploadCapturedPhoto();
                }
                else
                {
                    ClosePhotoOverlay(keepCapturedPhoto: true, restoreCurrentView: true, status: "Ready");
                }

                break;
        }
    }

    private async void BeginCapturePhotoAsync()
    {
        if (_capturePhotoInFlight)
        {
            return;
        }

        if (photoCapture == null)
        {
            uiPresenter?.SetStatus("Camera unavailable");
            ClosePhotoOverlay(keepCapturedPhoto: false, restoreCurrentView: true, status: "Camera unavailable");
            return;
        }

        _capturePhotoInFlight = true;
        _photoOverlayState = PhotoOverlayState.Busy;
        RenderPhotoOverlay();

        try
        {
            byte[] jpegBytes = await photoCapture.CaptureJpegAsync(photoMaxDimension, photoJpegQuality);
            if (jpegBytes == null || jpegBytes.Length == 0)
            {
                uiPresenter?.SetStatus("Photo capture failed");
                OpenCapturePrompt();
                return;
            }

            _capturedPhotoBytes = jpegBytes;
            _capturedPhotoWidth = photoCapture.LastCaptureWidth;
            _capturedPhotoHeight = photoCapture.LastCaptureHeight;
            _capturedPhotoMime = string.IsNullOrWhiteSpace(photoCapture.LastCaptureMimeType) ? "image/jpeg" : photoCapture.LastCaptureMimeType;

            if (debugPhotoLogs)
            {
                Debug.Log(
                    $"[ArGlassesClinicalAssistant] Photo captured bytes={_capturedPhotoBytes.Length} " +
                    $"size={_capturedPhotoWidth}x{_capturedPhotoHeight} mime={_capturedPhotoMime}");
            }

            OpenPhotoReviewPrompt();
        }
        catch (Exception ex)
        {
            Debug.LogWarning($"[ArGlassesClinicalAssistant] Photo capture exception: {ex.Message}");
            uiPresenter?.SetStatus("Photo capture failed");
            OpenCapturePrompt();
        }
        finally
        {
            _capturePhotoInFlight = false;
        }
    }

    private void UploadCapturedPhoto()
    {
        if (_capturedPhotoBytes == null || _capturedPhotoBytes.Length == 0)
        {
            uiPresenter?.SetStatus("No photo to upload");
            OpenCapturePrompt();
            return;
        }

        if (_lastTurnAudioForPhoto == null || _lastTurnAudioForPhoto.Length == 0)
        {
            uiPresenter?.SetStatus("No turn audio for photo upload");
            ClosePhotoOverlay(keepCapturedPhoto: false, restoreCurrentView: true, status: "No turn audio");
            return;
        }

        if (_recording)
        {
            uiPresenter?.SetStatus("Stop recording first");
            return;
        }

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            uiPresenter?.SetStatus("Offline - cannot upload photo");
            OpenPhotoReviewPrompt();
            return;
        }

        if (string.IsNullOrEmpty(_sessionId))
        {
            uiPresenter?.SetStatus("Session unavailable - restart app");
            OpenPhotoReviewPrompt();
            return;
        }

        _photoUploadInFlight = true;
        _processing = true;
        _photoOverlayState = PhotoOverlayState.Busy;
        RenderPhotoOverlay();

        if (debugPhotoLogs)
        {
            Debug.Log(
                $"[ArGlassesClinicalAssistant] Uploading photo-enhanced turn with audio_bytes={_lastTurnAudioForPhoto.Length} " +
                $"image_bytes={_capturedPhotoBytes.Length}");
        }

        SendProcessAudio(
            _lastTurnAudioForPhoto,
            _capturedPhotoBytes,
            _capturedPhotoMime,
            _capturedPhotoWidth,
            _capturedPhotoHeight);
    }

    private void ClosePhotoOverlay(bool keepCapturedPhoto, bool restoreCurrentView, string status)
    {
        _photoOverlayState = PhotoOverlayState.None;
        _photoOverlaySelection = 0;
        _photoAddPromptDeadline = -1f;
        _capturePhotoInFlight = false;
        _photoUploadInFlight = false;
        photoCapture?.StopPreview();

        if (!keepCapturedPhoto)
        {
            _capturedPhotoBytes = null;
            _capturedPhotoWidth = 0;
            _capturedPhotoHeight = 0;
            _capturedPhotoMime = "image/jpeg";
        }

        if (restoreCurrentView)
        {
            ApplyCurrentView();
        }

        if (!string.IsNullOrWhiteSpace(status))
        {
            uiPresenter?.SetStatus(status);
        }
    }

    private async void StartPhotoPreviewAsync()
    {
        if (photoCapture == null || _photoOverlayState != PhotoOverlayState.CapturePrompt)
        {
            return;
        }

        bool previewStarted = await photoCapture.StartPreviewAsync();
        if (_photoOverlayState != PhotoOverlayState.CapturePrompt)
        {
            photoCapture.StopPreview();
            return;
        }

        if (!previewStarted)
        {
            uiPresenter?.SetStatus("Camera unavailable");
        }
    }

    private void StartRecording()
    {
        Debug.Log("[ArGlassesClinicalAssistant] StartRecording requested.");
        if (_quitInProgress)
        {
            return;
        }

        if (IsPhotoOverlayActive())
        {
            uiPresenter?.SetStatus("Close photo dialog first");
            return;
        }

        if (_processing)
        {
            Debug.Log("[ArGlassesClinicalAssistant] StartRecording blocked: still processing previous turn.");
            uiPresenter?.SetStatus("Still processing previous turn.");
            return;
        }

        if (audioRecorder == null)
        {
            Debug.Log("[ArGlassesClinicalAssistant] StartRecording failed: audioRecorder is null.");
            uiPresenter?.SetStatus("Audio recorder is not configured.");
            return;
        }

        if (!audioRecorder.StartRecording())
        {
            Debug.Log("[ArGlassesClinicalAssistant] StartRecording failed: recorder returned false.");
            uiPresenter?.SetStatus("Unable to start microphone.");
            return;
        }

        _recording = true;
        _recordingStartedAt = Time.unscaledTime;
        uiPresenter?.SetRecordingState(true, 0f);
        uiPresenter?.SetStatus("Recording...");
        PlayStartEarcon();

        if (enableChunkedUpload)
        {
            PrepareChunkedTurn();
            TrySendAudioBeginIfReady();
        }

        Debug.Log("[ArGlassesClinicalAssistant] Recording started.");
    }

    private void StopRecordingAndProcess()
    {
        if (enableChunkedUpload)
        {
            StopRecordingAndProcessChunked();
            return;
        }

        StopRecordingAndProcessSingleShot();
    }

    private void StopRecordingAndProcessSingleShot()
    {
        Debug.Log("[ArGlassesClinicalAssistant] StopRecordingAndProcess requested.");
        if (audioRecorder == null)
        {
            Debug.Log("[ArGlassesClinicalAssistant] StopRecording skipped: audioRecorder is null.");
            return;
        }

        byte[] wav = audioRecorder.StopRecordingAsWav(out _);
        _recording = false;
        uiPresenter?.SetRecordingState(false, 0f);
        PlayStopEarcon();
        Debug.Log("[ArGlassesClinicalAssistant] Recording stopped.");

        if (wav == null || wav.Length == 0)
        {
            Debug.Log("[ArGlassesClinicalAssistant] No audio captured after stop.");
            uiPresenter?.SetStatus("No audio captured.");
            return;
        }

        Debug.Log($"[ArGlassesClinicalAssistant] Captured audio bytes: {wav.Length}");
        _lastBufferedAudio = wav;
        _lastTurnAudioForPhoto = wav;

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            Debug.Log("[ArGlassesClinicalAssistant] Upload failed immediately: socket offline.");
            _processing = false;
            uiPresenter?.SetStatus("Upload failed - retry");
            return;
        }

        _processing = true;
        uiPresenter?.SetStatus("Processing...");
        Debug.Log("[ArGlassesClinicalAssistant] Processing started.");

        if (string.IsNullOrEmpty(_sessionId))
        {
            Debug.Log("[ArGlassesClinicalAssistant] No session yet. Queue audio and request start_session.");
            _pendingAudioAfterSessionStart = wav;
            RequestStartSession();
            return;
        }

        Debug.Log("[ArGlassesClinicalAssistant] Sending process_audio request.");
        SendProcessAudio(wav);
    }

    private void StopRecordingAndProcessChunked()
    {
        Debug.Log("[ArGlassesClinicalAssistant] StopRecordingAndProcess (chunked) requested.");
        if (audioRecorder == null)
        {
            Debug.Log("[ArGlassesClinicalAssistant] StopRecording skipped: audioRecorder is null.");
            return;
        }

        if (_streamingTurnBegun)
        {
            PumpChunkStreaming(flush: true);
        }

        byte[] wav = audioRecorder.StopRecordingAsWav(out _);
        _recording = false;
        uiPresenter?.SetRecordingState(false, 0f);
        PlayStopEarcon();
        Debug.Log("[ArGlassesClinicalAssistant] Recording stopped.");

        if (wav == null || wav.Length == 0)
        {
            Debug.Log("[ArGlassesClinicalAssistant] No audio captured after stop.");
            uiPresenter?.SetStatus("No audio captured.");
            ResetChunkedTurnState();
            return;
        }

        _lastBufferedAudio = wav;
        _lastTurnAudioForPhoto = wav;
        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            Debug.Log("[ArGlassesClinicalAssistant] Upload failed immediately: socket offline.");
            _processing = false;
            uiPresenter?.SetStatus("Upload failed - retry");
            return;
        }

        if (!_streamingTurnBegun)
        {
            Debug.LogWarning("[ArGlassesClinicalAssistant] Chunk turn not started/acked before stop. Fallback to single-shot process_audio.");
            ResetChunkedTurnState();

            if (fallbackToSingleShotOnChunkFailure)
            {
                _processing = true;
                uiPresenter?.SetStatus("Processing...");
                SendProcessAudio(wav);
            }
            else
            {
                _processing = false;
                uiPresenter?.SetStatus("Chunk upload failed - retry");
            }

            return;
        }

        _processing = true;
        uiPresenter?.SetStatus("Processing...");
        SendAudioEnd();
    }

    private void PrepareChunkedTurn()
    {
        _activeTurnId = BuildTurnId();
        _nextChunkSeq = 1;
        _audioBeginInFlight = false;
        _streamingTurnBegun = false;
        _chunkBeginFailed = false;
        audioRecorder?.ResetPcmReadCursor();
    }

    private void ResetChunkedTurnState()
    {
        _activeTurnId = string.Empty;
        _nextChunkSeq = 1;
        _audioBeginInFlight = false;
        _streamingTurnBegun = false;
        _chunkBeginFailed = false;
    }

    private void TrySendAudioBeginIfReady()
    {
        if (!enableChunkedUpload || !_recording)
        {
            return;
        }

        if (_chunkBeginFailed)
        {
            return;
        }

        if (string.IsNullOrEmpty(_activeTurnId))
        {
            PrepareChunkedTurn();
        }

        if (_streamingTurnBegun || _audioBeginInFlight)
        {
            return;
        }

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            return;
        }

        if (string.IsNullOrEmpty(_sessionId))
        {
            RequestStartSession();
            return;
        }

        int sampleRate = audioRecorder != null ? audioRecorder.SampleRate : 16000;
        string requestId = NextRequestId("ab");
        Dictionary<string, object> payload = new Dictionary<string, object>
        {
            { "op", "audio_begin" },
            { "request_id", requestId },
            { "session_id", _sessionId },
            { "turn_id", _activeTurnId },
            { "audio_format", "pcm_s16le" },
            { "sample_rate", sampleRate },
            { "channels", 1 },
            { "chunk_ms", chunkMs }
        };

        _audioBeginInFlight = true;
        Debug.Log($"[ArGlassesClinicalAssistant] Sending audio_begin request_id={requestId} turn_id={_activeTurnId}");
        SendRequest(payload, requestId, "audio_begin", _activeTurnId);
    }

    private void HandleAudioAck(Dictionary<string, object> message)
    {
        _audioBeginInFlight = false;

        bool accepted = GetBool(message, "accepted", fallback: true);
        string turnId = GetString(message, "turn_id");
        if (!accepted)
        {
            Debug.LogWarning($"[ArGlassesClinicalAssistant] audio_begin rejected for turn_id={turnId}");
            _streamingTurnBegun = false;
            _chunkBeginFailed = true;
            return;
        }

        if (!string.IsNullOrEmpty(turnId) && !string.IsNullOrEmpty(_activeTurnId) && !string.Equals(turnId, _activeTurnId, StringComparison.Ordinal))
        {
            Debug.LogWarning($"[ArGlassesClinicalAssistant] audio_ack turn mismatch. expected={_activeTurnId} actual={turnId}");
            return;
        }

        _streamingTurnBegun = true;
        _chunkBeginFailed = false;
        Debug.Log($"[ArGlassesClinicalAssistant] audio_begin accepted turn_id={_activeTurnId}");
        PumpChunkStreaming(flush: false);
    }

    private void HandleChunkAck(Dictionary<string, object> message)
    {
        if (!logChunkTraffic)
        {
            return;
        }

        string turnId = GetString(message, "turn_id");
        int seq = GetInt(message, "seq", -1);
        int bytes = GetInt(message, "received_bytes", -1);
        Debug.Log($"[ArGlassesClinicalAssistant] chunk_ack turn_id={turnId} seq={seq} bytes={bytes}");
    }

    private void PumpChunkStreaming(bool flush)
    {
        if (!enableChunkedUpload || !_recording || !_streamingTurnBegun || audioRecorder == null)
        {
            return;
        }

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            return;
        }

        int targetFrames = GetChunkFrameCount();
        int sentCount = 0;

        while (audioRecorder.TryDequeuePcm16Chunk(targetFrames, flush, out byte[] pcmChunk, out int emittedFrames))
        {
            SendAudioChunk(pcmChunk, emittedFrames);
            sentCount++;

            // Keep per-frame work bounded during live recording.
            if (!flush && sentCount >= 4)
            {
                break;
            }
        }
    }

    private void SendAudioChunk(byte[] pcmChunk, int emittedFrames)
    {
        if (pcmChunk == null || pcmChunk.Length == 0)
        {
            return;
        }

        if (workflowSocket == null || !workflowSocket.IsConnected || string.IsNullOrEmpty(_sessionId) || string.IsNullOrEmpty(_activeTurnId))
        {
            return;
        }

        int seq = _nextChunkSeq++;
        string requestId = NextRequestId("ac");
        Dictionary<string, object> payload = new Dictionary<string, object>
        {
            { "op", "audio_chunk" },
            { "request_id", requestId },
            { "session_id", _sessionId },
            { "turn_id", _activeTurnId },
            { "seq", seq },
            { "audio_b64", Convert.ToBase64String(pcmChunk) }
        };

        if (logChunkTraffic)
        {
            Debug.Log($"[ArGlassesClinicalAssistant] audio_chunk turn_id={_activeTurnId} seq={seq} frames={emittedFrames} bytes={pcmChunk.Length}");
        }

        workflowSocket.SendText(ArSimpleJson.Serialize(payload));
    }

    private void SendAudioEnd()
    {
        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            _processing = false;
            uiPresenter?.SetStatus("Upload failed - retry");
            return;
        }

        if (string.IsNullOrEmpty(_sessionId) || string.IsNullOrEmpty(_activeTurnId))
        {
            Debug.LogWarning("[ArGlassesClinicalAssistant] audio_end skipped: missing session or turn id.");
            if (fallbackToSingleShotOnChunkFailure && _lastBufferedAudio != null && _lastBufferedAudio.Length > 0)
            {
                SendProcessAudio(_lastBufferedAudio);
            }
            else
            {
                _processing = false;
                uiPresenter?.SetStatus("Chunk upload failed - retry");
            }

            ResetChunkedTurnState();
            return;
        }

        string requestId = NextRequestId("ae");
        Dictionary<string, object> payload = new Dictionary<string, object>
        {
            { "op", "audio_end" },
            { "request_id", requestId },
            { "session_id", _sessionId },
            { "turn_id", _activeTurnId },
            { "return", BuildDefaultReturnFields() }
        };

        Debug.Log($"[ArGlassesClinicalAssistant] Sending audio_end request_id={requestId} turn_id={_activeTurnId}");
        SendRequest(payload, requestId, "audio_end", _activeTurnId);
    }

    private int GetChunkFrameCount()
    {
        int sampleRate = audioRecorder != null ? audioRecorder.SampleRate : 16000;
        float chunkSeconds = Mathf.Max(0.1f, chunkMs / 1000f);
        return Mathf.Max(1, Mathf.RoundToInt(sampleRate * chunkSeconds));
    }

    private static string BuildTurnId()
    {
        return $"t_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}_{Guid.NewGuid():N}";
    }

    private void ApplyCurrentView()
    {
        if (uiPresenter == null)
        {
            WarnMissingUiPresenter("ApplyCurrentView");
            return;
        }

        uiPresenter.SetHeader(CurrentHeader());

        string key = CurrentViewKey();
        bool scrollable = true;
        if (_cache.TryGetValue(key, out string content) && !string.IsNullOrWhiteSpace(content))
        {
            uiPresenter.SetContent(content, scrollable);
            return;
        }

        if (_turnIndex <= 0)
        {
            uiPresenter.SetContent("No result yet.", scrollable: false);
            return;
        }

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            uiPresenter.SetContent("Offline - cannot fetch this view.", scrollable: false);
            return;
        }

        if (!HasPendingGetLatest(key))
        {
            RequestGetLatest(key);
        }

        uiPresenter.SetContent("Loading...", scrollable: false);
    }

    private void WarnMissingUiPresenter(string caller)
    {
        if (_warnedMissingUiPresenter)
        {
            return;
        }

        _warnedMissingUiPresenter = true;
        Debug.LogWarning($"[ArGlassesClinicalAssistant] uiPresenter is null ({caller}). UI status/recording/content will not update.");
    }

    private void RequestStartSession()
    {
        if (_quitInProgress)
        {
            return;
        }

        if (!string.IsNullOrEmpty(_sessionId))
        {
            return;
        }

        EnsureLaunchPatientIdentity();

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            uiPresenter?.SetStatus("Offline - retrying session start.");
            return;
        }

        if (HasPendingOperation("start_session"))
        {
            return;
        }

        string requestId = NextRequestId("s");
        Dictionary<string, object> payload = new Dictionary<string, object>
        {
            { "op", "start_session" },
            { "request_id", requestId },
            { "patient_id", patientId }
        };
        Debug.Log($"[ArGlassesClinicalAssistant] Sending start_session request_id={requestId} patient_id={patientId}");
        SendRequest(payload, requestId, "start_session", string.Empty);
    }

    private void SendProcessAudio(
        byte[] wavAudio,
        byte[] imageBytes = null,
        string imageMime = null,
        int imageWidth = 0,
        int imageHeight = 0)
    {
        if (wavAudio == null || wavAudio.Length == 0)
        {
            uiPresenter?.SetStatus("No audio available for upload.");
            _processing = false;
            return;
        }

        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            _processing = false;
            uiPresenter?.SetStatus("Upload failed - retry");
            return;
        }

        if (string.IsNullOrEmpty(_sessionId))
        {
            _pendingAudioAfterSessionStart = wavAudio;
            RequestStartSession();
            return;
        }

        int sampleRate = audioRecorder != null ? audioRecorder.SampleRate : 16000;
        string requestId = NextRequestId("t");
        Dictionary<string, object> payload = new Dictionary<string, object>
        {
            { "op", "process_audio" },
            { "request_id", requestId },
            { "session_id", _sessionId },
            { "audio_b64", Convert.ToBase64String(wavAudio) },
            { "sample_rate", sampleRate },
            { "return", BuildDefaultReturnFields() }
        };

        if (imageBytes != null && imageBytes.Length > 0)
        {
            payload["image_b64"] = Convert.ToBase64String(imageBytes);
            payload["image_mime"] = string.IsNullOrWhiteSpace(imageMime) ? "image/jpeg" : imageMime;
            if (imageWidth > 0)
            {
                payload["image_width"] = imageWidth;
            }

            if (imageHeight > 0)
            {
                payload["image_height"] = imageHeight;
            }
        }

        if (imageBytes != null && imageBytes.Length > 0)
        {
            Debug.Log(
                $"[ArGlassesClinicalAssistant] Sending process_audio(+image) request_id={requestId} session_id={_sessionId} " +
                $"audio_bytes={wavAudio.Length} image_bytes={imageBytes.Length} mime={payload["image_mime"]}");
        }
        else
        {
            Debug.Log($"[ArGlassesClinicalAssistant] Sending process_audio request_id={requestId} session_id={_sessionId} bytes={wavAudio.Length}");
        }

        SendRequest(payload, requestId, "process_audio", "note_full");
    }

    private static List<object> BuildDefaultReturnFields()
    {
        return new List<object>
        {
            "note_full",
            "advice_full",
            "summary_turn",
            "running_summary"
        };
    }

    private void RequestGetLatest(string what)
    {
        if (string.IsNullOrEmpty(_sessionId) || _turnIndex <= 0)
        {
            return;
        }

        string requestId = NextRequestId("g");
        Dictionary<string, object> payload = new Dictionary<string, object>
        {
            { "op", "get_latest" },
            { "request_id", requestId },
            { "session_id", _sessionId },
            { "what", what }
        };
        SendRequest(payload, requestId, "get_latest", what);
        uiPresenter?.SetStatus("Loading view...");
    }

    private void SendRequest(Dictionary<string, object> payload, string requestId, string op, string what)
    {
        if (workflowSocket == null || !workflowSocket.IsConnected)
        {
            uiPresenter?.SetStatus("Offline - request not sent.");
            return;
        }

        _pendingRequests[requestId] = new PendingRequest
        {
            Op = op,
            What = what,
            StartedAt = Time.unscaledTime
        };

        Debug.Log($"[ArGlassesClinicalAssistant] WS send op={op} request_id={requestId} what={what}");
        string json = ArSimpleJson.Serialize(payload);
        workflowSocket.SendText(json);
    }

    private void CheckRequestTimeouts()
    {
        if (_pendingRequests.Count == 0)
        {
            return;
        }

        _timedOutRequestIds.Clear();
        float now = Time.unscaledTime;
        foreach (KeyValuePair<string, PendingRequest> pair in _pendingRequests)
        {
            if (now - pair.Value.StartedAt >= requestTimeoutSeconds)
            {
                _timedOutRequestIds.Add(pair.Key);
            }
        }

        foreach (string requestId in _timedOutRequestIds)
        {
            PendingRequest timedOut = _pendingRequests[requestId];
            _pendingRequests.Remove(requestId);

            if (timedOut.Op == "process_audio")
            {
                _processing = false;
                if (_photoUploadInFlight)
                {
                    _photoUploadInFlight = false;
                    uiPresenter?.SetStatus("Photo upload timeout");
                    if (_photoOverlayState == PhotoOverlayState.Busy)
                    {
                        OpenPhotoReviewPrompt();
                    }
                }
                else
                {
                    uiPresenter?.SetStatus("Timeout - try again");
                }
            }
            else if (timedOut.Op == "audio_end")
            {
                _processing = false;
                uiPresenter?.SetStatus("Timeout - try again");
            }
            else if (timedOut.Op == "audio_begin")
            {
                _audioBeginInFlight = false;
                uiPresenter?.SetStatus("Recording...");
            }
            else if (timedOut.Op == "get_latest")
            {
                uiPresenter?.SetStatus("Timeout loading view");
            }
            else if (timedOut.Op == "start_session")
            {
                _processing = false;
                uiPresenter?.SetStatus("Session start timeout - retrying");
            }
            else if (timedOut.Op == "end_session_quit")
            {
                PerformQuitNow();
            }
        }
    }

    private string NextRequestId(string prefix)
    {
        _requestCounter++;
        return $"{prefix}{_requestCounter}";
    }

    private static string InferOpFromRequestId(string requestId)
    {
        if (string.IsNullOrEmpty(requestId))
        {
            return string.Empty;
        }

        if (requestId.StartsWith("ab", StringComparison.Ordinal))
        {
            return "audio_begin";
        }

        if (requestId.StartsWith("ac", StringComparison.Ordinal))
        {
            return "audio_chunk";
        }

        if (requestId.StartsWith("ae", StringComparison.Ordinal))
        {
            return "audio_end";
        }

        if (requestId.StartsWith("s", StringComparison.Ordinal))
        {
            return "start_session";
        }

        if (requestId.StartsWith("t", StringComparison.Ordinal))
        {
            return "process_audio";
        }

        if (requestId.StartsWith("g", StringComparison.Ordinal))
        {
            return "get_latest";
        }

        if (requestId.StartsWith("eq", StringComparison.Ordinal))
        {
            return "end_session_quit";
        }

        if (requestId.StartsWith("e", StringComparison.Ordinal))
        {
            return "end_session";
        }

        return string.Empty;
    }

    private bool HasPendingOperation(string op)
    {
        foreach (PendingRequest pending in _pendingRequests.Values)
        {
            if (string.Equals(pending.Op, op, StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    private bool HasPendingGetLatest(string what)
    {
        foreach (PendingRequest pending in _pendingRequests.Values)
        {
            if (string.Equals(pending.Op, "get_latest", StringComparison.Ordinal)
                && string.Equals(pending.What, what, StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    private void InvalidateSession(string reason)
    {
        if (!string.IsNullOrEmpty(_sessionId))
        {
            Debug.LogWarning($"[ArGlassesClinicalAssistant] Invalidating session {_sessionId}: {reason}");
        }

        _sessionId = string.Empty;
        _turnIndex = 0;
        _cache.Clear();
        _audioBeginInFlight = false;
        _streamingTurnBegun = false;
        _chunkBeginFailed = false;
        _pendingQuitRequestId = string.Empty;
    }

    private static bool IsUnknownSessionError(string detail)
    {
        if (string.IsNullOrWhiteSpace(detail))
        {
            return false;
        }

        string text = detail.Trim().ToLowerInvariant();
        return text.Contains("unknown session_id")
            || text.Contains("unknown session")
            || (text.Contains("session_id") && text.Contains("not found"))
            || (text.Contains("session") && text.Contains("not found"))
            || text.Contains("session closed")
            || text.Contains("session expired");
    }

    private void TryCacheValue(Dictionary<string, object> message, string key)
    {
        if (!message.TryGetValue(key, out object value) || value == null)
        {
            return;
        }

        string text = value is string str ? NormalizeText(str) : FormatObjectForDisplay(value);
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        _cache[key] = text;
    }

    private string CurrentHeader()
    {
        string mode = _mode == ViewMode.Note ? "NOTE" : "ADVICE";
        return mode;
    }

    private ViewPage CurrentViewPage()
    {
        if (_mode == ViewMode.Note)
        {
            return ViewPage.NoteFull;
        }

        return ViewPage.AdviceFull;
    }

    private static ViewPage GetNextViewPage(ViewPage current)
    {
        return current == ViewPage.NoteFull ? ViewPage.AdviceFull : ViewPage.NoteFull;
    }

    private void SetViewPage(ViewPage page)
    {
        switch (page)
        {
            case ViewPage.NoteShort:
            case ViewPage.NoteFull:
                _mode = ViewMode.Note;
                _detail = DetailLevel.Full;
                break;
            case ViewPage.AdviceShort:
            case ViewPage.AdviceFull:
                _mode = ViewMode.Advice;
                _detail = DetailLevel.Full;
                break;
        }
    }

    private static string ViewPageLabel(ViewPage page)
    {
        switch (page)
        {
            case ViewPage.NoteShort:
            case ViewPage.NoteFull:
                return "NOTE_FULL";
            case ViewPage.AdviceShort:
            case ViewPage.AdviceFull:
                return "ADVICE_FULL";
            default:
                return "NOTE_FULL";
        }
    }

    private string CurrentViewKey()
    {
        return _mode == ViewMode.Note ? "note_full" : "advice_full";
    }

    private static string GetString(Dictionary<string, object> map, string key)
    {
        if (!map.TryGetValue(key, out object value) || value == null)
        {
            return string.Empty;
        }

        if (value is string str)
        {
            return str;
        }

        return Convert.ToString(value, CultureInfo.InvariantCulture) ?? string.Empty;
    }

    private static int GetInt(Dictionary<string, object> map, string key, int fallback)
    {
        if (!map.TryGetValue(key, out object value) || value == null)
        {
            return fallback;
        }

        if (value is long longValue)
        {
            return (int)longValue;
        }

        if (value is double doubleValue)
        {
            return (int)Math.Round(doubleValue);
        }

        if (int.TryParse(Convert.ToString(value, CultureInfo.InvariantCulture), NumberStyles.Integer, CultureInfo.InvariantCulture, out int parsed))
        {
            return parsed;
        }

        return fallback;
    }

    private static bool GetBool(Dictionary<string, object> map, string key, bool fallback)
    {
        if (!map.TryGetValue(key, out object value) || value == null)
        {
            return fallback;
        }

        if (value is bool boolValue)
        {
            return boolValue;
        }

        if (value is long longValue)
        {
            return longValue != 0;
        }

        if (value is double doubleValue)
        {
            return Math.Abs(doubleValue) > double.Epsilon;
        }

        if (bool.TryParse(Convert.ToString(value, CultureInfo.InvariantCulture), out bool parsed))
        {
            return parsed;
        }

        return fallback;
    }

    private static string NormalizeText(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        return value.Replace("\r\n", "\n").Replace('\r', '\n').Trim();
    }

    private static string FormatObjectForDisplay(object value)
    {
        StringBuilder builder = new StringBuilder();
        AppendFormatted(value, builder, 0);
        return builder.ToString().Trim();
    }

    private static void AppendFormatted(object value, StringBuilder builder, int indent)
    {
        string prefix = new string(' ', indent * 2);
        if (value == null)
        {
            builder.Append(prefix).Append("(null)").Append('\n');
            return;
        }

        if (value is string strValue)
        {
            builder.Append(prefix).Append(NormalizeText(strValue)).Append('\n');
            return;
        }

        if (value is IDictionary dict)
        {
            foreach (object key in dict.Keys)
            {
                object child = dict[key];
                if (IsPrimitive(child))
                {
                    builder.Append(prefix).Append(key).Append(": ").Append(PrimitiveToString(child)).Append('\n');
                }
                else
                {
                    builder.Append(prefix).Append(key).Append(':').Append('\n');
                    AppendFormatted(child, builder, indent + 1);
                }
            }

            return;
        }

        if (value is IList list)
        {
            if (list.Count == 0)
            {
                builder.Append(prefix).Append("- (none)").Append('\n');
                return;
            }

            foreach (object item in list)
            {
                if (IsPrimitive(item))
                {
                    builder.Append(prefix).Append("- ").Append(PrimitiveToString(item)).Append('\n');
                }
                else
                {
                    builder.Append(prefix).Append("-").Append('\n');
                    AppendFormatted(item, builder, indent + 1);
                }
            }

            return;
        }

        builder.Append(prefix).Append(PrimitiveToString(value)).Append('\n');
    }

    private static bool IsPrimitive(object value)
    {
        return value == null
            || value is string
            || value is bool
            || value is sbyte
            || value is byte
            || value is short
            || value is ushort
            || value is int
            || value is uint
            || value is long
            || value is ulong
            || value is float
            || value is double
            || value is decimal;
    }

    private static string PrimitiveToString(object value)
    {
        if (value == null)
        {
            return "(null)";
        }

        if (value is bool boolValue)
        {
            return boolValue ? "true" : "false";
        }

        if (value is string strValue)
        {
            return NormalizeText(strValue);
        }

        return Convert.ToString(value, CultureInfo.InvariantCulture) ?? string.Empty;
    }

    private void BuildEarcons()
    {
        _startEarconClip = BuildToneClip("ar_start_earcon", startBeepFrequency, beepDurationSeconds);
        _stopEarconClip = BuildDoubleToneClip("ar_stop_earcon", stopBeepFrequency, beepDurationSeconds, stopBeepGapSeconds);
    }

    private static AudioClip BuildToneClip(string clipName, float frequencyHz, float durationSeconds)
    {
        const int outputSampleRate = 44100;
        int sampleCount = Mathf.Max(1, Mathf.RoundToInt(outputSampleRate * durationSeconds));
        float[] samples = new float[sampleCount];

        for (int i = 0; i < sampleCount; i++)
        {
            float t = i / (float)outputSampleRate;
            float envelope = 1f - (i / (float)sampleCount);
            samples[i] = Mathf.Sin(2f * Mathf.PI * frequencyHz * t) * envelope * 0.35f;
        }

        AudioClip clip = AudioClip.Create(clipName, sampleCount, 1, outputSampleRate, stream: false);
        clip.SetData(samples, 0);
        return clip;
    }

    private static AudioClip BuildDoubleToneClip(string clipName, float frequencyHz, float beepDurationSeconds, float gapSeconds)
    {
        const int outputSampleRate = 44100;
        int beepSamples = Mathf.Max(1, Mathf.RoundToInt(outputSampleRate * beepDurationSeconds));
        int gapSamples = Mathf.Max(1, Mathf.RoundToInt(outputSampleRate * gapSeconds));
        int totalSamples = beepSamples * 2 + gapSamples;

        float[] samples = new float[totalSamples];
        WriteTone(samples, 0, beepSamples, outputSampleRate, frequencyHz);
        WriteTone(samples, beepSamples + gapSamples, beepSamples, outputSampleRate, frequencyHz);

        AudioClip clip = AudioClip.Create(clipName, totalSamples, 1, outputSampleRate, stream: false);
        clip.SetData(samples, 0);
        return clip;
    }

    private static void WriteTone(float[] output, int startIndex, int sampleCount, int sampleRate, float frequencyHz)
    {
        for (int i = 0; i < sampleCount; i++)
        {
            int dst = startIndex + i;
            if (dst < 0 || dst >= output.Length)
            {
                continue;
            }

            float t = i / (float)sampleRate;
            float envelope = 1f - (i / (float)sampleCount);
            output[dst] = Mathf.Sin(2f * Mathf.PI * frequencyHz * t) * envelope * 0.35f;
        }
    }

    private void PlayStartEarcon()
    {
        if (silentMode || earconSource == null || _startEarconClip == null)
        {
            return;
        }

        earconSource.PlayOneShot(_startEarconClip, earconVolume);
    }

    private void PlayStopEarcon()
    {
        if (silentMode || earconSource == null || _stopEarconClip == null)
        {
            return;
        }

        earconSource.PlayOneShot(_stopEarconClip, earconVolume);
    }
}
