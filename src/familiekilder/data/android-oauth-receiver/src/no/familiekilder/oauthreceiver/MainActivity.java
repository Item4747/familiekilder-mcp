package no.familiekilder.oauthreceiver;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.TextView;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

public final class MainActivity extends Activity {
    private static final String AUTHORIZE_ACTION =
            "no.familiekilder.oauthreceiver.AUTHORIZE";
    private static final String AUTHORIZATION_SCHEME = "https";
    private static final String BRIDGE_URL = "http://127.0.0.1:38443/v1/oauth-callback";
    private static final int MAX_CALLBACK_BYTES = 8192;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        processIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        processIntent(intent);
    }

    private void processIntent(Intent intent) {
        Uri uri = intent == null ? null : intent.getData();
        String action = intent == null ? null : intent.getAction();
        if (AUTHORIZE_ACTION.equals(action) && isAuthorizationUri(uri)) {
            startAuthorization(uri);
            return;
        }
        if (isCallbackUri(uri)) {
            deliverCallback(uri);
            return;
        }
        showMessage("Ugyldig innloggingsforespørsel. Gå tilbake til Familiekilder.");
    }

    private boolean isAuthorizationUri(Uri uri) {
        if (uri == null
                || !AUTHORIZATION_SCHEME.equals(uri.getScheme())
                || uri.getUserInfo() != null
                || uri.getPort() != -1) {
            return false;
        }
        boolean vigilo = "auth.prod.vigilo-oas.no".equals(uri.getHost())
                && "/connect/authorize".equals(uri.getPath());
        boolean skolemelding = "login.idporten.no".equals(uri.getHost())
                && "/authorize".equals(uri.getPath());
        return (vigilo || skolemelding)
                && uri.getUserInfo() == null
                && uri.getPort() == -1
                && uri.getQuery() != null;
    }

    private boolean isCallbackUri(Uri uri) {
        if (uri == null || uri.getUserInfo() != null || uri.getPort() != -1) {
            return false;
        }
        boolean vigilo = "app".equals(uri.getScheme())
                && "ch-parent-android.vigilo.no".equals(uri.getHost());
        boolean skolemelding = "com.cgi.oslomelding.foresatte".equals(uri.getScheme())
                && "oauth".equals(uri.getHost());
        return (vigilo || skolemelding)
                && uri.getUserInfo() == null
                && uri.getPort() == -1;
    }

    private void startAuthorization(Uri authorizationUri) {
        Intent browser = new Intent(Intent.ACTION_VIEW, authorizationUri);
        startActivity(browser);
        showMessage("Fullfør innloggingen i nettleseren.");
    }

    private void deliverCallback(Uri callback) {
        showMessage("Kontrollerer innloggingen …");

        if (!isCallbackUri(callback)) {
            showMessage("Ugyldig callback. Gå tilbake til Familiekilder.");
            return;
        }

        final byte[] body = callback.toString().getBytes(StandardCharsets.UTF_8);
        callback = null;
        if (body.length == 0 || body.length > MAX_CALLBACK_BYTES) {
            showMessage("Ugyldig callback. Gå tilbake til Familiekilder.");
            return;
        }

        new Thread(() -> deliver(body), "oauth-callback-delivery").start();
    }

    private void deliver(byte[] body) {
        boolean delivered = false;
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(BRIDGE_URL).openConnection();
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(5000);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setUseCaches(false);
            connection.setRequestProperty("Content-Type", "application/octet-stream");
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
            delivered = connection.getResponseCode() == 204;
        } catch (Exception ignored) {
            delivered = false;
        } finally {
            java.util.Arrays.fill(body, (byte) 0);
            if (connection != null) {
                connection.disconnect();
            }
        }

        final boolean result = delivered;
        runOnUiThread(() -> showMessage(
                result
                        ? "Innloggingen er levert. Du kan gå tilbake til Familiekilder."
                        : "Kunne ikke levere innloggingen. Gå tilbake til Familiekilder."
        ));
    }

    private void showMessage(String message) {
        TextView view = new TextView(this);
        view.setGravity(Gravity.CENTER);
        view.setPadding(48, 48, 48, 48);
        view.setText(message);
        view.setTextSize(18);
        setContentView(view);
    }
}
