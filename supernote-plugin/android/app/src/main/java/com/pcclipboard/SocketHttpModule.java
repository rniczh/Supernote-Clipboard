package com.pcclipboard;

import androidx.annotation.Nullable;

import com.facebook.react.bridge.Arguments;
import com.facebook.react.bridge.Promise;
import com.facebook.react.bridge.ReactApplicationContext;
import com.facebook.react.bridge.ReactContextBaseJavaModule;
import com.facebook.react.bridge.ReactMethod;
import com.facebook.react.bridge.ReadableMap;
import com.facebook.react.bridge.ReadableMapKeySetIterator;
import com.facebook.react.bridge.WritableMap;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.NoRouteToHostException;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Minimal HTTP client over a plain socket, used for every request to Supernote Clipboard.
 *
 * The computer app speaks plain HTTP on the local network, and the Supernote plugin host does not
 * permit cleartext traffic through the Android HTTP stacks (OkHttp, HttpURLConnection) that fetch,
 * Image and react-native-fs use. Sockets are not subject to that policy.
 */
public class SocketHttpModule extends ReactContextBaseJavaModule {

    private static final int MAX_TEXT_BODY = 1 << 20;
    private static final int MAX_HEADER_LINE = 8192;

    private final ExecutorService executor = Executors.newCachedThreadPool();

    public SocketHttpModule(ReactApplicationContext context) {
        super(context);
    }

    @Override
    public String getName() {
        return "PCHttp";
    }

    @Override
    public void invalidate() {
        executor.shutdownNow();
        super.invalidate();
    }

    /** Resolves {status, body} with the body as text. */
    @ReactMethod
    public void request(String url, String method, ReadableMap headers, @Nullable String body, double timeoutMs,
                        Promise promise) {
        executor.execute(() -> {
            try (Response response = Response.open(url, method, headers, body, (int) timeoutMs)) {
                WritableMap result = Arguments.createMap();
                result.putInt("status", response.status);
                result.putString("body", new String(response.readBody(MAX_TEXT_BODY), StandardCharsets.UTF_8));
                promise.resolve(result);
            } catch (Exception e) {
                reject(promise, e);
            }
        });
    }

    /** Saves the body to path when the status is 200. Resolves {status, body}; body is set only on errors. */
    @ReactMethod
    public void download(String url, ReadableMap headers, String path, double timeoutMs, Promise promise) {
        executor.execute(() -> {
            File target = new File(path);
            try (Response response = Response.open(url, "GET", headers, null, (int) timeoutMs)) {
                WritableMap result = Arguments.createMap();
                result.putInt("status", response.status);
                if (response.status == 200) {
                    File parent = target.getParentFile();
                    if (parent != null) {
                        parent.mkdirs();
                    }
                    try (OutputStream out = new FileOutputStream(target)) {
                        response.copyBody(out);
                    }
                } else {
                    result.putString("body",
                            new String(response.readBody(MAX_TEXT_BODY), StandardCharsets.UTF_8));
                }
                promise.resolve(result);
            } catch (Exception e) {
                target.delete();
                reject(promise, e);
            }
        });
    }

    private static void reject(Promise promise, Exception e) {
        String message = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
        if (e instanceof SocketTimeoutException) {
            promise.reject("E_TIMEOUT", "timed out", e);
        } else if (e instanceof NoRouteToHostException || message.contains("EHOSTUNREACH")
                || message.contains("ENETUNREACH")) {
            promise.reject("E_UNREACHABLE", "no route to that address", e);
        } else if (e instanceof ConnectException || message.contains("ECONNREFUSED")) {
            promise.reject("E_REFUSED", "connection refused", e);
        } else {
            promise.reject("E_HTTP", message, e);
        }
    }

    private static final class Response implements Closeable {
        final Socket socket;
        final InputStream in;
        final int status;
        final long contentLength;

        private Response(Socket socket, InputStream in, int status, long contentLength) {
            this.socket = socket;
            this.in = in;
            this.status = status;
            this.contentLength = contentLength;
        }

        static Response open(String url, String method, ReadableMap headers, @Nullable String body, int timeoutMs)
                throws IOException {
            URI uri = URI.create(url);
            if (!"http".equals(uri.getScheme()) || uri.getHost() == null) {
                throw new IOException("unsupported address " + url);
            }
            int port = uri.getPort() == -1 ? 80 : uri.getPort();
            String path = uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath();
            if (uri.getRawQuery() != null) {
                path += "?" + uri.getRawQuery();
            }
            byte[] payload = body == null ? new byte[0] : body.getBytes(StandardCharsets.UTF_8);

            StringBuilder head = new StringBuilder()
                    .append(method).append(' ').append(path).append(" HTTP/1.1\r\n")
                    .append("Host: ").append(uri.getHost()).append(':').append(port).append("\r\n")
                    .append("Connection: close\r\n");
            ReadableMapKeySetIterator keys = headers.keySetIterator();
            while (keys.hasNextKey()) {
                String key = keys.nextKey();
                head.append(key).append(": ").append(headers.getString(key)).append("\r\n");
            }
            if (body != null) {
                head.append("Content-Length: ").append(payload.length).append("\r\n");
            }
            head.append("\r\n");

            Socket socket = new Socket();
            try {
                socket.connect(new InetSocketAddress(uri.getHost(), port), timeoutMs);
                socket.setSoTimeout(timeoutMs);
                OutputStream out = socket.getOutputStream();
                out.write(head.toString().getBytes(StandardCharsets.UTF_8));
                out.write(payload);
                out.flush();

                InputStream in = new BufferedInputStream(socket.getInputStream());
                String[] statusLine = readLine(in).split(" ", 3);
                if (statusLine.length < 2 || !statusLine[0].startsWith("HTTP/")) {
                    throw new IOException("not an HTTP server");
                }
                int status = Integer.parseInt(statusLine[1]);
                long contentLength = -1;
                for (String line = readLine(in); !line.isEmpty(); line = readLine(in)) {
                    int colon = line.indexOf(':');
                    if (colon > 0 && line.substring(0, colon).trim().equalsIgnoreCase("Content-Length")) {
                        contentLength = Long.parseLong(line.substring(colon + 1).trim());
                    }
                }
                return new Response(socket, in, status, contentLength);
            } catch (IOException | RuntimeException e) {
                socket.close();
                throw e;
            }
        }

        private static String readLine(InputStream in) throws IOException {
            ByteArrayOutputStream line = new ByteArrayOutputStream();
            while (true) {
                int b = in.read();
                if (b == -1) {
                    throw new IOException("connection closed by the computer");
                }
                if (b == '\n') {
                    break;
                }
                if (b != '\r') {
                    line.write(b);
                }
                if (line.size() > MAX_HEADER_LINE) {
                    throw new IOException("response header too long");
                }
            }
            return line.toString("ISO-8859-1");
        }

        byte[] readBody(int max) throws IOException {
            ByteArrayOutputStream body = new ByteArrayOutputStream();
            copy(body, max);
            return body.toByteArray();
        }

        void copyBody(OutputStream out) throws IOException {
            copy(out, Long.MAX_VALUE);
        }

        private void copy(OutputStream out, long max) throws IOException {
            long remaining = contentLength >= 0 ? contentLength : Long.MAX_VALUE;
            long total = 0;
            byte[] buffer = new byte[64 * 1024];
            while (remaining > 0) {
                int n = in.read(buffer, 0, (int) Math.min(buffer.length, remaining));
                if (n == -1) {
                    if (contentLength >= 0) {
                        throw new IOException("download interrupted");
                    }
                    break;
                }
                total += n;
                if (total > max) {
                    throw new IOException("response too large");
                }
                out.write(buffer, 0, n);
                remaining -= n;
            }
        }

        @Override
        public void close() throws IOException {
            socket.close();
        }
    }
}
