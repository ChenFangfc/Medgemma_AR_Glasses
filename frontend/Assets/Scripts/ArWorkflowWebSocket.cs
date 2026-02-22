using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

public class ArWorkflowWebSocket : MonoBehaviour
{
    [SerializeField] private string serverUrl = "ws://127.0.0.1:8003/ws";
    [SerializeField] private bool connectOnStart;
    [SerializeField] private bool autoReconnect = true;
    [SerializeField] private float reconnectDelaySeconds = 2f;

    private readonly ConcurrentQueue<Action> _mainThreadQueue = new ConcurrentQueue<Action>();
    private ClientWebSocket _socket;
    private CancellationTokenSource _socketCts;
    private bool _connectInFlight;
    private bool _isQuitting;
    private float _nextReconnectAt;

    public event Action Connected;
    public event Action Disconnected;
    public event Action<string> TextMessageReceived;
    public event Action<string> Error;

    public bool IsConnected => _socket != null && _socket.State == WebSocketState.Open;

    public string ServerUrl
    {
        get => serverUrl;
        set => serverUrl = value;
    }

    private void Start()
    {
        _nextReconnectAt = Time.unscaledTime;
        if (connectOnStart)
        {
            Connect();
        }
    }

    private void Update()
    {
        while (_mainThreadQueue.TryDequeue(out Action action))
        {
            action?.Invoke();
        }

        if (autoReconnect && !_isQuitting && !IsConnected && !_connectInFlight && Time.unscaledTime >= _nextReconnectAt)
        {
            Connect();
        }
    }

    private void OnApplicationQuit()
    {
        _isQuitting = true;
    }

    private void OnDestroy()
    {
        _isQuitting = true;
        _ = DisconnectAsync();
    }

    public async void Connect()
    {
        await ConnectAsync();
    }

    public async Task ConnectAsync()
    {
        if (_isQuitting || _connectInFlight || IsConnected)
        {
            return;
        }

        if (string.IsNullOrWhiteSpace(serverUrl))
        {
            Enqueue(() => Error?.Invoke("WebSocket URL is empty."));
            ScheduleReconnect();
            return;
        }

        _connectInFlight = true;
        string targetUrl = serverUrl;
        Debug.Log($"WebSocket connect attempt: {targetUrl}");
        try
        {
            await CloseCurrentSocketAsync(notifyDisconnected: false);

            _socketCts = new CancellationTokenSource();
            ClientWebSocket socket = new ClientWebSocket();
            await socket.ConnectAsync(new Uri(targetUrl), _socketCts.Token);

            _socket = socket;
            Enqueue(() => Connected?.Invoke());

            _ = ReceiveLoopAsync(socket, _socketCts.Token);
        }
        catch (Exception ex)
        {
            Enqueue(() => Error?.Invoke($"WebSocket connect failed ({targetUrl}): {ex.Message}"));
            ScheduleReconnect();
        }
        finally
        {
            _connectInFlight = false;
        }
    }

    public async void SendText(string json)
    {
        await SendTextAsync(json);
    }

    public async Task SendTextAsync(string json)
    {
        if (!IsConnected)
        {
            Enqueue(() => Error?.Invoke("WebSocket send skipped: client is offline."));
            return;
        }

        byte[] bytes = Encoding.UTF8.GetBytes(json);
        ArraySegment<byte> segment = new ArraySegment<byte>(bytes);

        try
        {
            await _socket.SendAsync(segment, WebSocketMessageType.Text, endOfMessage: true, _socketCts.Token);
        }
        catch (Exception ex)
        {
            Enqueue(() => Error?.Invoke($"WebSocket send failed: {ex.Message}"));
            await CloseCurrentSocketAsync(notifyDisconnected: true);
        }
    }

    public async Task DisconnectAsync()
    {
        await CloseCurrentSocketAsync(notifyDisconnected: false);
    }

    private async Task ReceiveLoopAsync(ClientWebSocket socket, CancellationToken cancellationToken)
    {
        byte[] buffer = new byte[8192];
        ArraySegment<byte> segment = new ArraySegment<byte>(buffer);

        try
        {
            using (MemoryStream message = new MemoryStream())
            {
                while (!cancellationToken.IsCancellationRequested && socket.State == WebSocketState.Open)
                {
                    WebSocketReceiveResult result = await socket.ReceiveAsync(segment, cancellationToken);

                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        break;
                    }

                    if (result.MessageType != WebSocketMessageType.Text)
                    {
                        continue;
                    }

                    message.Write(buffer, 0, result.Count);
                    if (result.EndOfMessage)
                    {
                        string text = Encoding.UTF8.GetString(message.ToArray());
                        message.SetLength(0);
                        Enqueue(() => TextMessageReceived?.Invoke(text));
                    }
                }
            }
        }
        catch (OperationCanceledException)
        {
            // Normal on dispose/cancel.
        }
        catch (Exception ex)
        {
            Enqueue(() => Error?.Invoke($"WebSocket receive failed: {ex.Message}"));
        }
        finally
        {
            if (ReferenceEquals(socket, _socket))
            {
                await CloseCurrentSocketAsync(notifyDisconnected: true);
            }
        }
    }

    private async Task CloseCurrentSocketAsync(bool notifyDisconnected)
    {
        ClientWebSocket socket = _socket;
        _socket = null;

        if (socket != null)
        {
            try
            {
                if (socket.State == WebSocketState.Open || socket.State == WebSocketState.CloseReceived)
                {
                    await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "closing", CancellationToken.None);
                }
            }
            catch
            {
                // Ignore close failures.
            }

            socket.Dispose();
        }

        if (_socketCts != null)
        {
            _socketCts.Cancel();
            _socketCts.Dispose();
            _socketCts = null;
        }

        if (notifyDisconnected && socket != null)
        {
            Enqueue(() => Disconnected?.Invoke());
            ScheduleReconnect();
        }
    }

    private void ScheduleReconnect()
    {
        _nextReconnectAt = Time.unscaledTime + Mathf.Max(0.1f, reconnectDelaySeconds);
    }

    private void Enqueue(Action action)
    {
        _mainThreadQueue.Enqueue(action);
    }
}
