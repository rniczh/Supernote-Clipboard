package com.pcclipboard;

import com.facebook.react.bridge.Arguments;
import com.facebook.react.bridge.Promise;
import com.facebook.react.bridge.ReactApplicationContext;
import com.facebook.react.bridge.ReactContextBaseJavaModule;
import com.facebook.react.bridge.ReactMethod;
import com.facebook.react.bridge.WritableMap;
import com.facebook.react.modules.core.DeviceEventManagerModule;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * A tiny HTTP server so a phone on the same Wi-Fi can send a photo or copied text to this Supernote.
 *
 * The phone opens http://<supernote>:<port>/<token> (from the QR code), gets a page that prepares the
 * image, and posts it back. Every upload is reported to JavaScript as a "PCPhoneEvent". Only requests
 * carrying the session token are answered; stop() or a new start() invalidates it.
 */
public class PhoneServerModule extends ReactContextBaseJavaModule {

    static final String EVENT = "PCPhoneEvent";
    private static final int MAX_IMAGE_BYTES = 30 << 20;
    private static final int MAX_TEXT_BYTES = 256 << 10;
    private static final int MAX_HEADER_LINE = 8192;
    private static final int READ_TIMEOUT_MS = 60000;

    private final ExecutorService executor = Executors.newCachedThreadPool();
    private final SecureRandom random = new SecureRandom();
    private ServerSocket server;

    public PhoneServerModule(ReactApplicationContext context) {
        super(context);
    }

    @Override
    public String getName() {
        return "PCPhoneServer";
    }

    @Override
    public void invalidate() {
        closeServer();
        executor.shutdownNow();
        super.invalidate();
    }

    /** Starts (or restarts) the server. Resolves {port, token}. */
    @ReactMethod
    public void start(String page, String uploadDir, Promise promise) {
        try {
            closeServer();
            Session session = new Session(newToken(), page, new File(uploadDir));
            ServerSocket socket = new ServerSocket();
            socket.setReuseAddress(true);
            socket.bind(new InetSocketAddress(0));
            synchronized (this) {
                server = socket;
            }
            executor.execute(() -> acceptLoop(socket, session));
            WritableMap result = Arguments.createMap();
            result.putInt("port", socket.getLocalPort());
            result.putString("token", session.token);
            promise.resolve(result);
        } catch (Exception e) {
            promise.reject("E_PHONE_SERVER", e.getMessage() != null ? e.getMessage() : e.toString(), e);
        }
    }

    @ReactMethod
    public void stop(Promise promise) {
        closeServer();
        promise.resolve(null);
    }

    // Required by NativeEventEmitter.
    @ReactMethod
    public void addListener(String eventName) {
    }

    @ReactMethod
    public void removeListeners(double count) {
    }

    private synchronized void closeServer() {
        if (server != null) {
            try {
                server.close();
            } catch (IOException ignored) {
            }
            server = null;
        }
    }

    private String newToken() {
        byte[] bytes = new byte[16];
        random.nextBytes(bytes);
        StringBuilder token = new StringBuilder();
        for (byte b : bytes) {
            token.append(String.format("%02x", b));
        }
        return token.toString();
    }

    private void acceptLoop(ServerSocket socket, Session session) {
        while (!socket.isClosed()) {
            try {
                Socket client = socket.accept();
                executor.execute(() -> handle(client, session, socket));
            } catch (IOException e) {
                return; // closed
            }
        }
    }

    private static final class Session {
        final String token;
        final String page;
        final File uploadDir;

        Session(String token, String page, File uploadDir) {
            this.token = token;
            this.page = page;
            this.uploadDir = uploadDir;
        }

        boolean matches(String candidate) {
            return MessageDigest.isEqual(token.getBytes(StandardCharsets.US_ASCII),
                    candidate.getBytes(StandardCharsets.US_ASCII));
        }
    }

    private void handle(Socket client, Session session, ServerSocket owner) {
        try (Socket socket = client) {
            socket.setSoTimeout(READ_TIMEOUT_MS);
            InputStream in = new BufferedInputStream(socket.getInputStream());
            OutputStream out = socket.getOutputStream();
            String[] request = readLine(in).split(" ");
            if (request.length < 2) {
                return;
            }
            Map<String, String> headers = new HashMap<>();
            for (String line = readLine(in); !line.isEmpty(); line = readLine(in)) {
                int colon = line.indexOf(':');
                if (colon > 0) {
                    headers.put(line.substring(0, colon).trim().toLowerCase(), line.substring(colon + 1).trim());
                }
            }
            String method = request[0];
            String path = request[1];
            int query = path.indexOf('?');
            if (query >= 0) {
                path = path.substring(0, query);
            }
            String[] parts = path.split("/");
            // "/<token>" -> ["", token]; "/<token>/image" -> ["", token, "image"]
            if (owner.isClosed() || parts.length < 2 || !session.matches(parts[1])) {
                respond(out, 404, "text/plain; charset=utf-8",
                        "This link has expired. Tap \"Paste from phone\" on your Supernote again.");
                return;
            }
            String action = parts.length > 2 ? parts[2] : "";
            long length = parseLength(headers.get("content-length"));

            if (method.equals("GET") && action.isEmpty()) {
                respond(out, 200, "text/html; charset=utf-8", session.page);
                emit("connected", null, null);
            } else if (method.equals("POST") && action.equals("text")) {
                if (length <= 0 || length > MAX_TEXT_BYTES) {
                    respond(out, 413, "application/json", "{\"error\":\"The text is empty or too long.\"}");
                    return;
                }
                String text = new String(readBody(in, length), StandardCharsets.UTF_8);
                emit("text", null, text);
                respond(out, 200, "application/json", "{\"ok\":true}");
            } else if (method.equals("POST") && action.equals("image")) {
                if (length <= 0 || length > MAX_IMAGE_BYTES) {
                    respond(out, 413, "application/json", "{\"error\":\"The image is empty or too large.\"}");
                    return;
                }
                String type = headers.getOrDefault("content-type", "");
                String ext = type.contains("png") ? "png" : "jpg";
                session.uploadDir.mkdirs();
                File file = new File(session.uploadDir, "phone-" + System.currentTimeMillis() + "." + ext);
                try (OutputStream fileOut = new FileOutputStream(file)) {
                    copyBody(in, fileOut, length);
                } catch (IOException e) {
                    file.delete();
                    throw e;
                }
                emit("image", file.getAbsolutePath(), null);
                respond(out, 200, "application/json", "{\"ok\":true}");
            } else {
                respond(out, 404, "text/plain; charset=utf-8", "Not found.");
            }
        } catch (IOException ignored) {
            // The phone went away mid-request; nothing to report.
        }
    }

    private void emit(String kind, String path, String text) {
        WritableMap event = Arguments.createMap();
        event.putString("kind", kind);
        if (path != null) {
            event.putString("path", path);
        }
        if (text != null) {
            event.putString("text", text);
        }
        ReactApplicationContext context = getReactApplicationContext();
        if (context.hasActiveReactInstance()) {
            context.getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter.class).emit(EVENT, event);
        }
    }

    private static long parseLength(String value) {
        try {
            return value == null ? -1 : Long.parseLong(value);
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    private static void respond(OutputStream out, int status, String type, String body) throws IOException {
        byte[] payload = body.getBytes(StandardCharsets.UTF_8);
        String reason = status == 200 ? "OK" : status == 404 ? "Not Found" : "Error";
        String head = "HTTP/1.1 " + status + " " + reason + "\r\n"
                + "Content-Type: " + type + "\r\n"
                + "Content-Length: " + payload.length + "\r\n"
                + "Cache-Control: no-store\r\n"
                + "Connection: close\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.US_ASCII));
        out.write(payload);
        out.flush();
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        while (true) {
            int b = in.read();
            if (b == -1) {
                throw new IOException("connection closed");
            }
            if (b == '\n') {
                break;
            }
            if (b != '\r') {
                line.write(b);
            }
            if (line.size() > MAX_HEADER_LINE) {
                throw new IOException("header too long");
            }
        }
        return line.toString("ISO-8859-1");
    }

    private static byte[] readBody(InputStream in, long length) throws IOException {
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        copyBody(in, body, length);
        return body.toByteArray();
    }

    private static void copyBody(InputStream in, OutputStream out, long length) throws IOException {
        byte[] buffer = new byte[64 * 1024];
        long remaining = length;
        while (remaining > 0) {
            int n = in.read(buffer, 0, (int) Math.min(buffer.length, remaining));
            if (n == -1) {
                throw new IOException("upload interrupted");
            }
            out.write(buffer, 0, n);
            remaining -= n;
        }
    }
}
