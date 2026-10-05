package org.findmycar.app;

import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;

import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebViewClient;

import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(ArVioPlugin.class);
        super.onCreate(savedInstanceState);

        Bridge bridge = getBridge();
        if (bridge == null) return;

        // ngrok free interstitial blocks WebView unless this header is set
        bridge.getWebView().setWebViewClient(new BridgeWebViewClient(bridge) {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                String host = request.getUrl() != null ? request.getUrl().getHost() : null;
                if (host != null && host.contains("ngrok") && "GET".equalsIgnoreCase(request.getMethod())) {
                    WebResourceResponse bypass = fetchNgrok(request.getUrl().toString());
                    if (bypass != null) return bypass;
                }
                return super.shouldInterceptRequest(view, request);
            }
        });
    }

    private static WebResourceResponse fetchNgrok(String urlStr) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(urlStr);
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(30000);
            conn.setInstanceFollowRedirects(true);
            conn.setRequestProperty("ngrok-skip-browser-warning", "true");
            conn.setRequestProperty("User-Agent", "FindMyCarAndroid");
            int code = conn.getResponseCode();
            InputStream stream = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
            if (stream == null) return null;

            String mime = conn.getContentType();
            String encoding = "UTF-8";
            if (mime != null && mime.contains(";")) {
                String[] parts = mime.split(";");
                mime = parts[0].trim();
                for (int i = 1; i < parts.length; i++) {
                    String p = parts[i].trim();
                    if (p.toLowerCase().startsWith("charset=")) {
                        encoding = p.substring(8).trim();
                    }
                }
            }
            if (mime == null || mime.isEmpty()) mime = "text/plain";

            Map<String, String> headers = new HashMap<>();
            Map<String, List<String>> raw = conn.getHeaderFields();
            if (raw != null) {
                for (Map.Entry<String, List<String>> e : raw.entrySet()) {
                    if (e.getKey() != null && e.getValue() != null && !e.getValue().isEmpty()) {
                        headers.put(e.getKey(), e.getValue().get(0));
                    }
                }
            }
            return new WebResourceResponse(mime, encoding, code, "OK", headers, stream);
        } catch (IOException err) {
            return null;
        }
    }
}
