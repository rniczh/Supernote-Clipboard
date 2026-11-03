/**
 * The page a phone opens after scanning the QR code. It is served by the Supernote itself over
 * plain HTTP, so the browser's Clipboard API is unavailable (it needs HTTPS); copied content comes
 * in through a paste box instead. Images are scaled to the note's width, flattened onto white and
 * converted to grayscale on the phone, so only a small file crosses the network.
 */
export function phonePage(maxWidth: number, maxTextChars: number) {
  return PAGE.replace('__MAX_WIDTH__', String(maxWidth)).replace(
    '__MAX_TEXT__',
    String(maxTextChars),
  );
}

const PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send to Supernote</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 20px; font: 17px/1.45 -apple-system, system-ui, sans-serif;
         color: #111; background: #fff; max-width: 560px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 4px 0 18px; }
  h2 { font-size: 17px; margin: 28px 0 8px; }
  button { display: block; width: 100%; font: inherit; font-weight: 600; padding: 15px;
           margin: 10px 0; border: 2px solid #111; border-radius: 12px; background: #fff; color: #111; }
  button.primary { background: #111; color: #fff; }
  button:disabled { opacity: .4; }
  #box { min-height: 110px; max-height: 40vh; overflow: auto; padding: 12px; border: 2px dashed #777;
         border-radius: 12px; white-space: pre-wrap; word-break: break-word; outline: none; }
  #box:empty:before { content: "Long-press here and choose Paste, or type."; color: #888; }
  #status { min-height: 1.5em; margin-top: 18px; font-weight: 600; }
  #status.error { color: #b00020; }
  .hint { color: #555; font-size: 15px; }
  #preview { display: block; max-width: 100%; max-height: 30vh; margin: 14px auto 0; border: 1px solid #ccc; }
</style>
</head>
<body>
<h1>Send to Supernote</h1>
<button id="choose" class="primary">Choose photos</button>
<button id="camera">Take a photo</button>
<input id="chooseInput" type="file" accept="image/*" multiple hidden>
<input id="cameraInput" type="file" accept="image/*" capture="environment" hidden>

<h2>Or paste what you copied</h2>
<div id="box" contenteditable="true"></div>
<button id="sendText" disabled>Send text</button>
<p class="hint">Copied an image? Paste it in the box and it is sent right away.</p>

<p id="status"></p>
<img id="preview" alt="" hidden>

<script>
(function () {
  var MAX_WIDTH = __MAX_WIDTH__;
  var MAX_TEXT = __MAX_TEXT__;
  var BASE = location.pathname.replace(/\\/+$/, '');
  var statusEl = document.getElementById('status');
  var box = document.getElementById('box');
  var sendTextBtn = document.getElementById('sendText');
  var preview = document.getElementById('preview');
  var busy = false;

  function setStatus(text, error) {
    statusEl.textContent = text;
    statusEl.className = error ? 'error' : '';
  }

  function post(kind, body, type) {
    return fetch(BASE + '/' + kind, {method: 'POST', headers: {'Content-Type': type}, body: body})
      .then(function (res) {
        if (res.status === 404) {
          throw new Error('This link has expired. Tap "Paste from phone" on your Supernote and scan the new code.');
        }
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            throw new Error(data.error || ('The Supernote refused it (HTTP ' + res.status + ').'));
          });
        }
      }, function () {
        throw new Error('Could not reach the Supernote. Keep it on the same Wi-Fi as this phone and try again.');
      });
  }

  function loadImage(blob) {
    var viaElement = function () {
      return new Promise(function (resolve, reject) {
        var url = URL.createObjectURL(blob);
        var img = new Image();
        img.onload = function () { resolve(img); };
        img.onerror = function () {
          URL.revokeObjectURL(url);
          reject(new Error('This browser cannot open that image. Try a JPEG, PNG or a screenshot.'));
        };
        img.src = url;
      });
    };
    if (window.createImageBitmap) {
      return createImageBitmap(blob, {imageOrientation: 'from-image'}).catch(viaElement);
    }
    return viaElement();
  }

  function toGrayBlob(blob) {
    return loadImage(blob).then(function (img) {
      var w = img.width, h = img.height;
      if (!w || !h) { throw new Error('That image is empty.'); }
      var scale = Math.min(1, MAX_WIDTH / w);
      w = Math.max(1, Math.round(w * scale));
      h = Math.max(1, Math.round(h * scale));
      var canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      var ctx = canvas.getContext('2d');
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(img, 0, 0, w, h);
      var data = ctx.getImageData(0, 0, w, h);
      var px = data.data;
      for (var i = 0; i < px.length; i += 4) {
        var y = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
        px[i] = px[i + 1] = px[i + 2] = y;
      }
      ctx.putImageData(data, 0, 0);
      // Screenshots and drawings stay sharp as PNG; photos are much smaller as JPEG.
      var type = blob.type === 'image/png' || blob.type === 'image/gif' ? 'image/png' : 'image/jpeg';
      return new Promise(function (resolve, reject) {
        canvas.toBlob(function (out) {
          if (out) { resolve({blob: out, type: type}); } else { reject(new Error('Could not prepare the image.')); }
        }, type, 0.9);
      });
    });
  }

  function sendImages(files) {
    files = Array.prototype.filter.call(files, function (f) { return /^image\\//.test(f.type) || !f.type; });
    if (!files.length || busy) { return; }
    busy = true;
    var sent = 0;
    var next = function () {
      if (sent === files.length) {
        busy = false;
        setStatus(files.length === 1
          ? 'Photo sent. It is now on your Supernote page.'
          : files.length + ' photos sent. They are now on your Supernote page.');
        return;
      }
      setStatus(files.length === 1 ? 'Sending...' : 'Sending ' + (sent + 1) + ' of ' + files.length + '...');
      return toGrayBlob(files[sent]).then(function (prepared) {
        preview.src = URL.createObjectURL(prepared.blob);
        preview.hidden = false;
        return post('image', prepared.blob, prepared.type);
      }).then(function () {
        sent += 1;
        return next();
      });
    };
    Promise.resolve().then(next).catch(function (e) {
      busy = false;
      setStatus(e.message, true);
    });
  }

  function boxText() {
    return box.innerText.replace(/\\u00a0/g, ' ').replace(/\\s+$/, '');
  }

  function updateSendText() {
    sendTextBtn.disabled = busy || !boxText().trim();
  }

  document.getElementById('choose').onclick = function () { document.getElementById('chooseInput').click(); };
  document.getElementById('camera').onclick = function () { document.getElementById('cameraInput').click(); };
  ['chooseInput', 'cameraInput'].forEach(function (id) {
    var input = document.getElementById(id);
    input.onchange = function () {
      var files = Array.prototype.slice.call(input.files || []);
      input.value = '';
      sendImages(files);
    };
  });

  box.addEventListener('paste', function (event) {
    var data = event.clipboardData;
    if (!data) { return; }
    var images = [];
    Array.prototype.forEach.call(data.items || [], function (item) {
      if (item.kind === 'file' && /^image\\//.test(item.type)) {
        var file = item.getAsFile();
        if (file) { images.push(file); }
      }
    });
    event.preventDefault();
    if (images.length) {
      sendImages(images);
      return;
    }
    var text = data.getData('text/plain');
    if (text) {
      document.execCommand('insertText', false, text);
      updateSendText();
      setStatus('Check the text, then tap "Send text".');
    }
  });
  box.addEventListener('input', updateSendText);

  sendTextBtn.onclick = function () {
    var text = boxText();
    if (!text.trim() || busy) { return; }
    if (text.length > MAX_TEXT) {
      setStatus('That text is too long. Send at most ' + MAX_TEXT + ' characters at a time.', true);
      return;
    }
    busy = true;
    updateSendText();
    setStatus('Sending...');
    post('text', text, 'text/plain; charset=utf-8').then(function () {
      box.textContent = '';
      preview.hidden = true;
      setStatus('Text sent. It is now on your Supernote page.');
    }, function (e) {
      setStatus(e.message, true);
    }).then(function () {
      busy = false;
      updateSendText();
    });
  };
})();
</script>
</body>
</html>
`;
