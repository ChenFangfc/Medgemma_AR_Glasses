using System;
using System.Text;
using UnityEngine;

public class ArAudioRecorder : MonoBehaviour
{
    [SerializeField] private string microphoneDeviceName = "";
    [SerializeField] private int sampleRate = 16000;
    [SerializeField] private int maxRecordingSeconds = 120;

    private AudioClip _recordingClip;
    private string _activeDeviceName;
    private float _recordingStartedAt;
    private int _streamReadFrameIndex;

    public bool IsRecording { get; private set; }

    public int SampleRate => sampleRate;

    public float CurrentDurationSeconds
    {
        get
        {
            if (!IsRecording)
            {
                return 0f;
            }

            return Mathf.Max(0f, Time.unscaledTime - _recordingStartedAt);
        }
    }

    public bool StartRecording()
    {
        if (IsRecording)
        {
            return false;
        }

        if (Microphone.devices == null || Microphone.devices.Length == 0)
        {
            Debug.LogError("No microphone devices found.");
            return false;
        }

        _activeDeviceName = ResolveDeviceName();
        if (string.IsNullOrEmpty(_activeDeviceName))
        {
            Debug.LogError("Unable to resolve a microphone device.");
            return false;
        }

        _recordingClip = Microphone.Start(_activeDeviceName, loop: false, lengthSec: maxRecordingSeconds, frequency: sampleRate);
        if (_recordingClip == null)
        {
            Debug.LogError("Microphone.Start failed.");
            return false;
        }

        _streamReadFrameIndex = 0;
        IsRecording = true;
        _recordingStartedAt = Time.unscaledTime;
        return true;
    }

    public void ResetPcmReadCursor()
    {
        _streamReadFrameIndex = 0;
    }

    public bool TryDequeuePcm16Chunk(int targetFrameCount, bool flush, out byte[] pcmBytes, out int emittedFrames)
    {
        pcmBytes = null;
        emittedFrames = 0;

        if (!IsRecording || _recordingClip == null)
        {
            return false;
        }

        int clipFrames = _recordingClip.samples;
        if (clipFrames <= 0)
        {
            return false;
        }

        int writeFrame = Microphone.GetPosition(_activeDeviceName);
        if (writeFrame < 0)
        {
            return false;
        }

        writeFrame = Mathf.Clamp(writeFrame, 0, clipFrames);
        int availableFrames = ComputeAvailableFrames(_streamReadFrameIndex, writeFrame, clipFrames);
        if (availableFrames <= 0)
        {
            return false;
        }

        int wantedFrames = Mathf.Max(1, targetFrameCount);
        if (!flush && availableFrames < wantedFrames)
        {
            return false;
        }

        int framesToRead = flush ? availableFrames : Mathf.Min(wantedFrames, availableFrames);
        pcmBytes = ReadPcm16Frames(_streamReadFrameIndex, framesToRead);
        if (pcmBytes == null || pcmBytes.Length == 0)
        {
            return false;
        }

        _streamReadFrameIndex = (_streamReadFrameIndex + framesToRead) % clipFrames;
        emittedFrames = framesToRead;
        return true;
    }

    public byte[] StopRecordingAsWav(out float durationSeconds)
    {
        durationSeconds = 0f;

        if (!IsRecording || _recordingClip == null)
        {
            return null;
        }

        int channels = _recordingClip.channels;
        int clipSamples = _recordingClip.samples;
        int endPosition = Microphone.GetPosition(_activeDeviceName);
        if (endPosition <= 0 || endPosition > clipSamples)
        {
            int estimatedSamples = Mathf.RoundToInt(CurrentDurationSeconds * sampleRate);
            endPosition = Mathf.Clamp(estimatedSamples, 1, clipSamples);
        }

        float[] allSamples = new float[clipSamples * channels];
        _recordingClip.GetData(allSamples, 0);

        int sampleCount = Mathf.Clamp(endPosition * channels, 1, allSamples.Length);
        float[] clippedSamples = new float[sampleCount];
        Array.Copy(allSamples, clippedSamples, sampleCount);

        Microphone.End(_activeDeviceName);
        IsRecording = false;

        durationSeconds = (float)endPosition / sampleRate;

        AudioClip oldClip = _recordingClip;
        _recordingClip = null;
        _streamReadFrameIndex = 0;
        Destroy(oldClip);

        return ArWavEncoder.Encode(clippedSamples, channels, sampleRate);
    }

    private string ResolveDeviceName()
    {
        if (!string.IsNullOrWhiteSpace(microphoneDeviceName))
        {
            foreach (string device in Microphone.devices)
            {
                if (string.Equals(device, microphoneDeviceName, StringComparison.Ordinal))
                {
                    return device;
                }
            }
        }

        return Microphone.devices[0];
    }

    private static int ComputeAvailableFrames(int readFrame, int writeFrame, int clipFrames)
    {
        if (writeFrame >= readFrame)
        {
            return writeFrame - readFrame;
        }

        return (clipFrames - readFrame) + writeFrame;
    }

    private byte[] ReadPcm16Frames(int startFrame, int frameCount)
    {
        int channels = _recordingClip.channels;
        int clipFrames = _recordingClip.samples;

        if (frameCount <= 0 || channels <= 0 || clipFrames <= 0)
        {
            return null;
        }

        int firstPartFrames = Mathf.Min(frameCount, clipFrames - startFrame);
        int secondPartFrames = frameCount - firstPartFrames;

        float[] first = new float[firstPartFrames * channels];
        _recordingClip.GetData(first, startFrame);

        float[] second = null;
        if (secondPartFrames > 0)
        {
            second = new float[secondPartFrames * channels];
            _recordingClip.GetData(second, 0);
        }

        return ConvertFloatPcmToPcm16Bytes(first, second, channels);
    }

    private static byte[] ConvertFloatPcmToPcm16Bytes(float[] first, float[] second, int channels)
    {
        if (channels <= 0)
        {
            return null;
        }

        int firstFrames = first != null ? first.Length / channels : 0;
        int secondFrames = second != null ? second.Length / channels : 0;
        int totalFrames = firstFrames + secondFrames;
        if (totalFrames <= 0)
        {
            return null;
        }

        // Streamed chunks are always mono PCM16 (channels=1 in audio_begin).
        // If microphone input is multi-channel, average channels per frame.
        byte[] bytes = new byte[totalFrames * 2];
        int outIndex = 0;

        WriteFloatsAsMonoPcm16(first, channels, bytes, ref outIndex);
        WriteFloatsAsMonoPcm16(second, channels, bytes, ref outIndex);
        return bytes;
    }

    private static void WriteFloatsAsMonoPcm16(float[] samples, int channels, byte[] output, ref int outIndex)
    {
        if (samples == null || output == null || channels <= 0)
        {
            return;
        }

        int frameCount = samples.Length / channels;
        for (int frame = 0; frame < frameCount; frame++)
        {
            int baseIndex = frame * channels;
            float mixed = 0f;
            for (int ch = 0; ch < channels; ch++)
            {
                mixed += Mathf.Clamp(samples[baseIndex + ch], -1f, 1f);
            }

            mixed /= channels;
            short pcm = (short)Mathf.Clamp(Mathf.RoundToInt(mixed * 32767f), -32768, 32767);
            output[outIndex++] = (byte)(pcm & 0xff);
            output[outIndex++] = (byte)((pcm >> 8) & 0xff);
        }
    }
}

public static class ArWavEncoder
{
    private const int HeaderSize = 44;
    private const int BytesPerSample = 2;

    public static byte[] Encode(float[] samples, int channels, int sampleRate)
    {
        if (samples == null || samples.Length == 0)
        {
            return null;
        }

        int dataSize = samples.Length * BytesPerSample;
        byte[] wav = new byte[HeaderSize + dataSize];

        WriteAscii(wav, 0, "RIFF");
        WriteInt(wav, 4, HeaderSize - 8 + dataSize);
        WriteAscii(wav, 8, "WAVE");
        WriteAscii(wav, 12, "fmt ");
        WriteInt(wav, 16, 16);
        WriteShort(wav, 20, 1);
        WriteShort(wav, 22, (short)channels);
        WriteInt(wav, 24, sampleRate);
        WriteInt(wav, 28, sampleRate * channels * BytesPerSample);
        WriteShort(wav, 32, (short)(channels * BytesPerSample));
        WriteShort(wav, 34, 16);
        WriteAscii(wav, 36, "data");
        WriteInt(wav, 40, dataSize);

        int offset = HeaderSize;
        for (int i = 0; i < samples.Length; i++)
        {
            short pcm = (short)Mathf.RoundToInt(Mathf.Clamp(samples[i], -1f, 1f) * 32767f);
            wav[offset++] = (byte)(pcm & 0xff);
            wav[offset++] = (byte)((pcm >> 8) & 0xff);
        }

        return wav;
    }

    private static void WriteAscii(byte[] buffer, int offset, string value)
    {
        byte[] bytes = Encoding.ASCII.GetBytes(value);
        Buffer.BlockCopy(bytes, 0, buffer, offset, bytes.Length);
    }

    private static void WriteInt(byte[] buffer, int offset, int value)
    {
        buffer[offset] = (byte)(value & 0xff);
        buffer[offset + 1] = (byte)((value >> 8) & 0xff);
        buffer[offset + 2] = (byte)((value >> 16) & 0xff);
        buffer[offset + 3] = (byte)((value >> 24) & 0xff);
    }

    private static void WriteShort(byte[] buffer, int offset, short value)
    {
        buffer[offset] = (byte)(value & 0xff);
        buffer[offset + 1] = (byte)((value >> 8) & 0xff);
    }
}
