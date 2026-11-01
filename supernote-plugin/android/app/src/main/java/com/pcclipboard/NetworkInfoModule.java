package com.pcclipboard;

import com.facebook.react.bridge.Arguments;
import com.facebook.react.bridge.Promise;
import com.facebook.react.bridge.ReactApplicationContext;
import com.facebook.react.bridge.ReactContextBaseJavaModule;
import com.facebook.react.bridge.ReactMethod;
import com.facebook.react.bridge.WritableArray;
import com.facebook.react.bridge.WritableMap;

import java.net.Inet4Address;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.util.Collections;

/** Lists the device's own IPv4 addresses so the plugin can search its Wi-Fi subnet for the computer. */
public class NetworkInfoModule extends ReactContextBaseJavaModule {

    public NetworkInfoModule(ReactApplicationContext context) {
        super(context);
    }

    @Override
    public String getName() {
        return "PCNetworkInfo";
    }

    @ReactMethod
    public void getIPv4Addresses(Promise promise) {
        try {
            WritableArray result = Arguments.createArray();
            for (NetworkInterface iface : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!iface.isUp() || iface.isLoopback()) {
                    continue;
                }
                for (InterfaceAddress address : iface.getInterfaceAddresses()) {
                    if (!(address.getAddress() instanceof Inet4Address)) {
                        continue;
                    }
                    WritableMap entry = Arguments.createMap();
                    entry.putString("address", address.getAddress().getHostAddress());
                    entry.putInt("prefixLength", address.getNetworkPrefixLength());
                    entry.putString("interface", iface.getName());
                    result.pushMap(entry);
                }
            }
            promise.resolve(result);
        } catch (Exception e) {
            promise.reject("E_NETWORK_INFO", e);
        }
    }
}
