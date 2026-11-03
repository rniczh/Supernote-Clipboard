package com.pcclipboard;

import com.facebook.react.ReactPackage;
import com.facebook.react.bridge.NativeModule;
import com.facebook.react.bridge.ReactApplicationContext;
import com.facebook.react.uimanager.ViewManager;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

public class NetworkInfoPackage implements ReactPackage {

    @Override
    public List<NativeModule> createNativeModules(ReactApplicationContext context) {
        return Arrays.asList(new NetworkInfoModule(context), new SocketHttpModule(context),
                new PhoneServerModule(context));
    }

    @Override
    public List<ViewManager> createViewManagers(ReactApplicationContext context) {
        return Collections.emptyList();
    }
}
