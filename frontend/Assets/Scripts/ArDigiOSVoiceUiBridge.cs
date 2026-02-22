using System;
using System.Collections.Generic;
using UnityEngine;

public class ArDigiOSVoiceUiBridge : MonoBehaviour
{
    [Header("Dependencies")]
    [SerializeField] private ArGlassesClinicalAssistant assistant;

    [Header("Voice UI")]
    [SerializeField] private bool startOnAwake = true;
    [SerializeField] private string languageCode = "en";
    [SerializeField] private string callbackGameObjectName = "VoiceUI_Handler";
    [SerializeField] private bool debugLogs = true;

    [Header("Command Phrases")]
    [SerializeField] private string startRecordingCommand = "start recording";
    [SerializeField] private string stopRecordingCommand = "stop recording";
    [SerializeField] private string toggleRecordingCommand = "toggle recording";
    [SerializeField] private string showNoteCommand = "show note";
    [SerializeField] private string showAdviceCommand = "show advice";
    [SerializeField] private string showShortCommand = "show short";
    [SerializeField] private string showFullCommand = "show full";
    [SerializeField] private string retryUploadCommand = "retry upload";

    private AndroidJavaObject _voiceUiInterface;
    private AndroidJavaClass _voiceUiConstants;
    private AndroidJavaObject _voiceUiModel;
    private readonly List<AndroidJavaObject> _listeners = new List<AndroidJavaObject>();

    private void Awake()
    {
        if (!string.IsNullOrWhiteSpace(callbackGameObjectName) && gameObject.name != callbackGameObjectName)
        {
            gameObject.name = callbackGameObjectName;
        }

        if (assistant == null)
        {
            assistant = GetComponent<ArGlassesClinicalAssistant>();
        }
    }

    private void Start()
    {
        if (startOnAwake)
        {
            StartVoiceUi();
        }
    }

    private void OnDestroy()
    {
        StopVoiceUi();
    }

    public bool StartVoiceUi()
    {
#if UNITY_ANDROID && !UNITY_EDITOR
        if (_voiceUiInterface != null)
        {
            return true;
        }

        if (assistant == null)
        {
            Debug.LogError("ArDigiOSVoiceUiBridge: assistant reference is missing.");
            return false;
        }

        try
        {
            _voiceUiInterface = new AndroidJavaObject("com.digilens.digios_unity_plugin.VoiceUI_Interface");
            _voiceUiConstants = new AndroidJavaClass("com.digilens.digios_unity_plugin.utils.Constants");
            _voiceUiModel = new AndroidJavaObject("com.digilens.digios_unity_plugin.utils.VoiceUI_Model", languageCode);

            RegisterVoiceCommands();
            _voiceUiInterface.Call("add_model", _voiceUiModel);

            using (AndroidJavaClass unityPlayer = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
            {
                AndroidJavaObject activity = unityPlayer.GetStatic<AndroidJavaObject>("currentActivity");
                _voiceUiInterface.Call("start", activity);
            }

            if (debugLogs)
            {
                Debug.Log("DigiOS VoiceUI started.");
            }

            return true;
        }
        catch (Exception ex)
        {
            Debug.LogError($"Failed to start DigiOS VoiceUI: {ex.Message}");
            StopVoiceUi();
            return false;
        }
#else
        if (debugLogs)
        {
            Debug.Log("VoiceUI start skipped: only available on Android device.");
        }

        return false;
#endif
    }

    public void StopVoiceUi()
    {
#if UNITY_ANDROID && !UNITY_EDITOR
        try
        {
            if (_voiceUiInterface != null)
            {
                _voiceUiInterface.Call("stop");
            }
        }
        catch (Exception ex)
        {
            if (debugLogs)
            {
                Debug.LogWarning($"VoiceUI stop warning: {ex.Message}");
            }
        }

        DisposeListeners();
        DisposeObject(ref _voiceUiModel);
        DisposeObject(ref _voiceUiConstants);
        DisposeObject(ref _voiceUiInterface);
#endif
    }

    public void VoiceUI_OnCommand(string voiceCommand)
    {
        if (assistant == null)
        {
            return;
        }

        string command = (voiceCommand ?? string.Empty).Trim();
        if (debugLogs)
        {
            Debug.Log($"VoiceUI command: {command}");
        }

        if (EqualsCommand(command, startRecordingCommand))
        {
            assistant.StartRecordingIfIdle();
            return;
        }

        if (EqualsCommand(command, stopRecordingCommand))
        {
            assistant.StopRecordingIfActive();
            return;
        }

        if (EqualsCommand(command, toggleRecordingCommand))
        {
            assistant.ToggleRecording();
            return;
        }

        if (EqualsCommand(command, showNoteCommand))
        {
            assistant.ShowNote();
            return;
        }

        if (EqualsCommand(command, showAdviceCommand))
        {
            assistant.ShowAdvice();
            return;
        }

        if (EqualsCommand(command, showShortCommand))
        {
            assistant.ShowShort();
            return;
        }

        if (EqualsCommand(command, showFullCommand))
        {
            assistant.ShowFull();
            return;
        }

        if (EqualsCommand(command, retryUploadCommand))
        {
            assistant.RetryLastUpload();
        }
    }

    private void RegisterVoiceCommands()
    {
        int feedbackOnly = _voiceUiConstants.GetStatic<int>("Voice_Command_CONFIG_TYPE_FEEDBACK_ONLY");

        RegisterCommand(startRecordingCommand, feedbackOnly);
        RegisterCommand(stopRecordingCommand, feedbackOnly);
        RegisterCommand(toggleRecordingCommand, feedbackOnly);
        RegisterCommand(showNoteCommand, feedbackOnly);
        RegisterCommand(showAdviceCommand, feedbackOnly);
        RegisterCommand(showShortCommand, feedbackOnly);
        RegisterCommand(showFullCommand, feedbackOnly);
        RegisterCommand(retryUploadCommand, feedbackOnly);
    }

    private void RegisterCommand(string phrase, int configType)
    {
        if (string.IsNullOrWhiteSpace(phrase))
        {
            return;
        }

        AndroidJavaObject listener = new AndroidJavaObject(
            "com.digilens.digios_unity_plugin.utils.VoiceUI_Listener",
            phrase,
            configType,
            gameObject.name,
            "VoiceUI_OnCommand");

        _listeners.Add(listener);
        _voiceUiModel.Call("addVoiceUI_Listener", listener);
    }

    private void DisposeListeners()
    {
        for (int i = 0; i < _listeners.Count; i++)
        {
            AndroidJavaObject listener = _listeners[i];
            DisposeObject(ref listener);
        }

        _listeners.Clear();
    }

    private static void DisposeObject<T>(ref T obj) where T : class, IDisposable
    {
        if (obj == null)
        {
            return;
        }

        obj.Dispose();
        obj = null;
    }

    private static bool EqualsCommand(string a, string b)
    {
        return string.Equals(a?.Trim(), b?.Trim(), StringComparison.OrdinalIgnoreCase);
    }
}
