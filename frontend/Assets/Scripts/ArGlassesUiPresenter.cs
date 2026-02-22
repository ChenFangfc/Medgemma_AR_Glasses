using System;
using System.Text;
using System.Text.RegularExpressions;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

[Serializable]
public class ArTextTarget
{
    [SerializeField] private Text uiText;
    [SerializeField] private TMP_Text tmpText;

    public void Set(string value)
    {
        if (uiText != null)
        {
            uiText.text = value;
        }

        if (tmpText != null)
        {
            tmpText.text = value;
        }
    }

    public RectTransform GetRectTransform()
    {
        if (tmpText != null)
        {
            return tmpText.rectTransform;
        }

        if (uiText != null)
        {
            return uiText.rectTransform;
        }

        return null;
    }

    public void ConfigureForLongText()
    {
        if (uiText != null)
        {
            uiText.horizontalOverflow = HorizontalWrapMode.Wrap;
            uiText.verticalOverflow = VerticalWrapMode.Overflow;
        }

        if (tmpText != null)
        {
            tmpText.enableWordWrapping = true;
            tmpText.overflowMode = TextOverflowModes.Overflow;
        }
    }

    public void ForceLayoutRefresh()
    {
        if (tmpText != null)
        {
            tmpText.ForceMeshUpdate(ignoreActiveState: true, forceTextReparsing: false);
        }
    }
}

public class ArGlassesUiPresenter : MonoBehaviour
{
    [Header("Text Targets")]
    [SerializeField] private ArTextTarget headerText;
    [SerializeField] private ArTextTarget bodyText;
    [SerializeField] private ArTextTarget statusText;
    [SerializeField] private ArTextTarget connectivityText;
    [SerializeField] private ArTextTarget recordingText;

    [Header("Layout")]
    [SerializeField] private int shortViewMaxLines = 6;
    [SerializeField] private int fullViewLinesPerPage = 10;
    [SerializeField] private ScrollRect bodyScrollRect;
    [SerializeField] private bool preferScrollRectForFullViews = true;
    [SerializeField] [Range(0.01f, 0.5f)] private float scrollRectStep = 0.05f;
    [SerializeField] private bool autoCreateBodyScrollRect = true;

    [Header("Recording Pulse")]
    [SerializeField] private CanvasGroup recordingPulseGroup;
    [SerializeField] private float pulseSpeed = 2f;
    [SerializeField] private float pulseMinAlpha = 0.35f;
    [SerializeField] private float pulseMaxAlpha = 1f;

    private string[] _contentLines = Array.Empty<string>();
    private int _scrollLineIndex;
    private bool _currentViewScrollable;
    private bool _recording;
    private float _recordingSeconds;

    private void Awake()
    {
        EnsureBodyScrollRect();
    }

    private void Update()
    {
        UpdateRecordingIndicator();
    }

    public void SetHeader(string value)
    {
        headerText?.Set(value ?? string.Empty);
    }

    public void SetContent(string value, bool scrollable)
    {
        EnsureBodyScrollRect();

        string normalized = NormalizeForDisplay(value);
        _contentLines = normalized.Split('\n');
        _currentViewScrollable = scrollable;
        _scrollLineIndex = 0;
        bodyText?.ConfigureForLongText();

        if (_currentViewScrollable && preferScrollRectForFullViews && bodyScrollRect != null)
        {
            bodyText?.Set(normalized);
            ForceBodyLayoutRebuild();
            bodyScrollRect.verticalNormalizedPosition = 1f;
            bodyScrollRect.StopMovement();
            return;
        }

        RenderContent();
    }

    public void Scroll(float delta)
    {
        if (!_currentViewScrollable || _contentLines.Length == 0)
        {
            return;
        }

        if (preferScrollRectForFullViews && bodyScrollRect != null)
        {
            if (!TryGetBodyScrollRects(out RectTransform contentRect, out RectTransform viewportRect))
            {
                return;
            }

            ForceBodyLayoutRebuild();
            if (!HasScrollableOverflow(contentRect, viewportRect))
            {
                return;
            }

            float position = bodyScrollRect.verticalNormalizedPosition;
            float step = Mathf.Clamp(scrollRectStep, 0.01f, 0.5f) * Mathf.Clamp(Mathf.Abs(delta), 1f, 3f);
            if (delta > 0f)
            {
                position = Mathf.Min(1f, position + step);
            }
            else if (delta < 0f)
            {
                position = Mathf.Max(0f, position - step);
            }

            bodyScrollRect.verticalNormalizedPosition = position;
            bodyScrollRect.StopMovement();
            return;
        }

        int pageSize = Mathf.Max(1, fullViewLinesPerPage);
        int maxStart = Mathf.Max(0, _contentLines.Length - pageSize);

        if (delta > 0f)
        {
            _scrollLineIndex = Mathf.Max(0, _scrollLineIndex - 1);
        }
        else if (delta < 0f)
        {
            _scrollLineIndex = Mathf.Min(maxStart, _scrollLineIndex + 1);
        }

        RenderContent();
    }

    public void SetStatus(string value)
    {
        statusText?.Set(value ?? string.Empty);
    }

    public void SetConnectivity(bool online)
    {
        connectivityText?.Set(online ? "Online" : "Offline");
    }

    public void SetRecordingState(bool recording, float elapsedSeconds)
    {
        _recording = recording;
        _recordingSeconds = Mathf.Max(0f, elapsedSeconds);

        if (!_recording)
        {
            recordingText?.Set(string.Empty);
            if (recordingPulseGroup != null)
            {
                recordingPulseGroup.alpha = 0f;
            }
        }
    }

    private void RenderContent()
    {
        if (_contentLines == null || _contentLines.Length == 0)
        {
            bodyText?.Set(string.Empty);
            return;
        }

        int pageSize = _currentViewScrollable ? Mathf.Max(1, fullViewLinesPerPage) : Mathf.Max(1, shortViewMaxLines);
        int startLine = _currentViewScrollable ? _scrollLineIndex : 0;
        int endLine = Mathf.Min(_contentLines.Length, startLine + pageSize);

        StringBuilder builder = new StringBuilder();
        for (int i = startLine; i < endLine; i++)
        {
            if (builder.Length > 0)
            {
                builder.Append('\n');
            }

            builder.Append(_contentLines[i]);
        }

        bodyText?.Set(builder.ToString());
    }

    private void UpdateRecordingIndicator()
    {
        if (!_recording)
        {
            return;
        }

        int totalSeconds = Mathf.FloorToInt(_recordingSeconds);
        int minutes = totalSeconds / 60;
        int seconds = totalSeconds % 60;
        recordingText?.Set($"REC {minutes:00}:{seconds:00}");

        if (recordingPulseGroup != null)
        {
            float wave = 0.5f + 0.5f * Mathf.Sin(Time.unscaledTime * pulseSpeed * Mathf.PI * 2f);
            recordingPulseGroup.alpha = Mathf.Lerp(pulseMinAlpha, pulseMaxAlpha, wave);
        }
    }

    private static string Normalize(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        return value.Replace("\r\n", "\n").Replace('\r', '\n');
    }

    private static string NormalizeForDisplay(string value)
    {
        string normalized = Normalize(value);
        if (string.IsNullOrWhiteSpace(normalized))
        {
            return string.Empty;
        }

        string output = normalized.Replace("•", "·");
        output = Regex.Replace(output, @"\s*·\s+", "\n· ");
        return output.Trim();
    }

    private void ForceBodyLayoutRebuild()
    {
        bodyText?.ForceLayoutRefresh();

        RectTransform bodyRect = bodyText?.GetRectTransform();
        if (bodyRect == null)
        {
            return;
        }

        Canvas.ForceUpdateCanvases();
        LayoutRebuilder.ForceRebuildLayoutImmediate(bodyRect);

        if (bodyScrollRect != null && bodyScrollRect.viewport != null)
        {
            LayoutRebuilder.ForceRebuildLayoutImmediate(bodyScrollRect.viewport);
        }

        Canvas.ForceUpdateCanvases();
    }

    private void EnsureBodyScrollRect()
    {
        if (!preferScrollRectForFullViews)
        {
            return;
        }

        RectTransform bodyRect = bodyText?.GetRectTransform();
        if (bodyRect == null)
        {
            return;
        }

        if (bodyScrollRect != null)
        {
            EnsureExistingBodyScrollRect(bodyRect);
            return;
        }

        if (!autoCreateBodyScrollRect)
        {
            return;
        }

        RectTransform containerRect = bodyRect.parent as RectTransform;
        if (containerRect == null)
        {
            return;
        }

        int originalIndex = bodyRect.GetSiblingIndex();
        GameObject viewportObject = new GameObject("BodyViewport", typeof(RectTransform), typeof(RectMask2D), typeof(ScrollRect));
        RectTransform viewportRect = viewportObject.GetComponent<RectTransform>();
        viewportRect.SetParent(containerRect, worldPositionStays: false);
        viewportRect.SetSiblingIndex(originalIndex);

        // Preserve current body text bounds as viewport bounds.
        viewportRect.anchorMin = bodyRect.anchorMin;
        viewportRect.anchorMax = bodyRect.anchorMax;
        viewportRect.anchoredPosition = bodyRect.anchoredPosition;
        viewportRect.sizeDelta = bodyRect.sizeDelta;
        viewportRect.pivot = bodyRect.pivot;
        viewportRect.localScale = Vector3.one;
        viewportRect.localRotation = Quaternion.identity;

        bodyRect.SetParent(viewportRect, worldPositionStays: false);
        ConfigureBodyRectForScroll(bodyRect);

        bodyScrollRect = viewportObject.GetComponent<ScrollRect>();
        ConfigureScrollRect(bodyScrollRect, bodyRect, viewportRect);
    }

    private void EnsureExistingBodyScrollRect(RectTransform bodyRect)
    {
        if (!TryGetBodyScrollRects(out RectTransform _, out RectTransform viewportRect))
        {
            return;
        }

        if (bodyRect.parent != viewportRect)
        {
            bodyRect.SetParent(viewportRect, worldPositionStays: false);
        }

        ConfigureBodyRectForScroll(bodyRect);
        ConfigureScrollRect(bodyScrollRect, bodyRect, viewportRect);
    }

    private static void ConfigureBodyRectForScroll(RectTransform bodyRect)
    {
        bodyRect.anchorMin = new Vector2(0f, 1f);
        bodyRect.anchorMax = new Vector2(1f, 1f);
        bodyRect.pivot = new Vector2(0.5f, 1f);
        bodyRect.anchoredPosition = Vector2.zero;
        bodyRect.sizeDelta = Vector2.zero;
        bodyRect.localScale = Vector3.one;
        bodyRect.localRotation = Quaternion.identity;

        ContentSizeFitter fitter = bodyRect.GetComponent<ContentSizeFitter>();
        if (fitter == null)
        {
            fitter = bodyRect.gameObject.AddComponent<ContentSizeFitter>();
        }

        fitter.horizontalFit = ContentSizeFitter.FitMode.Unconstrained;
        fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;
    }

    private static void ConfigureScrollRect(ScrollRect scrollRect, RectTransform content, RectTransform viewport)
    {
        scrollRect.horizontal = false;
        scrollRect.vertical = true;
        scrollRect.movementType = ScrollRect.MovementType.Clamped;
        scrollRect.inertia = false;
        scrollRect.scrollSensitivity = 20f;
        scrollRect.content = content;
        scrollRect.viewport = viewport;
        scrollRect.verticalNormalizedPosition = 1f;
    }

    private bool TryGetBodyScrollRects(out RectTransform contentRect, out RectTransform viewportRect)
    {
        contentRect = null;
        viewportRect = null;
        if (bodyScrollRect == null)
        {
            return false;
        }

        contentRect = bodyScrollRect.content;
        if (contentRect == null)
        {
            contentRect = bodyText?.GetRectTransform();
            if (contentRect != null)
            {
                bodyScrollRect.content = contentRect;
            }
        }

        viewportRect = bodyScrollRect.viewport;
        if (viewportRect == null)
        {
            viewportRect = bodyScrollRect.transform as RectTransform;
            bodyScrollRect.viewport = viewportRect;
        }

        return contentRect != null && viewportRect != null;
    }

    private static bool HasScrollableOverflow(RectTransform contentRect, RectTransform viewportRect)
    {
        float contentHeight = LayoutUtility.GetPreferredHeight(contentRect);
        if (contentHeight <= 0f)
        {
            contentHeight = contentRect.rect.height;
        }

        float viewportHeight = viewportRect.rect.height;
        return contentHeight > viewportHeight + 1f;
    }
}
