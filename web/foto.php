<?php
declare(strict_types=1);

const MAX_IMAGE_BYTES = 8388608;
const MAX_IMAGE_SIDE = 2048;

function fail(int $status, string $message): void
{
    http_response_code($status);
    header('Content-Type: text/plain; charset=utf-8');
    echo $message;
    exit;
}

function configuration(): array
{
    $token = getenv('STREAMO_IMAGE_TOKEN');
    $dataDirectory = getenv('STREAMO_IMAGE_DATA_DIR');
    if ($token === false || strlen($token) < 20) {
        fail(500, 'STREAMO_IMAGE_TOKEN is not configured.');
    }
    if ($dataDirectory === false || !is_dir($dataDirectory)) {
        fail(500, 'STREAMO_IMAGE_DATA_DIR is not configured.');
    }
    return [$token, rtrim($dataDirectory, '/')];
}

function request_token(): string
{
    return isset($_GET['token']) && is_string($_GET['token']) ? $_GET['token'] : '';
}

function require_token(string $expected): void
{
    if (!hash_equals($expected, request_token())) {
        fail(404, 'Not found.');
    }
}

function manifest(string $dataDirectory)
{
    $path = $dataDirectory . '/images.jsonl';
    $manifest = fopen($path, 'c+');
    if ($manifest === false) {
        fail(500, 'The image feed cannot be opened.');
    }
    return $manifest;
}

function upload_image(string $dataDirectory): void
{
    if (isset($_SERVER['CONTENT_LENGTH']) && (int) $_SERVER['CONTENT_LENGTH'] > MAX_IMAGE_BYTES + 1048576) {
        fail(413, 'The upload is too large.');
    }
    if (!isset($_FILES['photo']) || !is_array($_FILES['photo'])) {
        fail(400, 'No photo was uploaded.');
    }
    $photo = $_FILES['photo'];
    if (!isset($photo['error'], $photo['size'], $photo['tmp_name']) || $photo['error'] !== UPLOAD_ERR_OK) {
        fail(400, 'The photo upload failed.');
    }
    if ((int) $photo['size'] < 1 || (int) $photo['size'] > MAX_IMAGE_BYTES) {
        fail(413, 'The upload is too large.');
    }
    $temporary = (string) $photo['tmp_name'];
    $information = @getimagesize($temporary);
    if ($information === false || $information[0] > MAX_IMAGE_SIDE || $information[1] > MAX_IMAGE_SIDE) {
        fail(400, 'The photo dimensions are invalid.');
    }
    $finfo = new finfo(FILEINFO_MIME_TYPE);
    if ($finfo->file($temporary) !== 'image/jpeg' || $information['mime'] !== 'image/jpeg') {
        fail(400, 'Only browser-prepared JPEG photos are accepted.');
    }

    $manifest = manifest($dataDirectory);
    if (!flock($manifest, LOCK_EX)) {
        fclose($manifest);
        fail(500, 'The image feed cannot be locked.');
    }
    $lastId = 0;
    rewind($manifest);
    while (($line = fgets($manifest)) !== false) {
        $item = json_decode($line, true);
        if (is_array($item) && isset($item['id']) && is_int($item['id'])) {
            $lastId = max($lastId, $item['id']);
        }
    }
    $id = $lastId + 1;
    $target = sprintf('%s/%08d.jpg', $dataDirectory, $id);
    if (!move_uploaded_file($temporary, $target)) {
        flock($manifest, LOCK_UN);
        fclose($manifest);
        fail(500, 'The photo could not be stored.');
    }
    chmod($target, 0640);
    fseek($manifest, 0, SEEK_END);
    $record = json_encode(['id' => $id], JSON_UNESCAPED_SLASHES) . "\n";
    if (fwrite($manifest, $record) !== strlen($record) || !fflush($manifest)) {
        unlink($target);
        flock($manifest, LOCK_UN);
        fclose($manifest);
        fail(500, 'The image feed could not be updated.');
    }
    flock($manifest, LOCK_UN);
    fclose($manifest);

    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode(['id' => $id]);
}

function send_feed(string $dataDirectory): void
{
    $after = isset($_GET['after']) ? filter_var($_GET['after'], FILTER_VALIDATE_INT) : 0;
    if ($after === false || $after < 0) {
        fail(400, 'Invalid feed cursor.');
    }
    $manifest = manifest($dataDirectory);
    if (!flock($manifest, LOCK_SH)) {
        fclose($manifest);
        fail(500, 'The image feed cannot be locked.');
    }
    header('Content-Type: application/x-ndjson; charset=utf-8');
    header('Cache-Control: no-store');
    rewind($manifest);
    while (($line = fgets($manifest)) !== false) {
        $item = json_decode($line, true);
        if (is_array($item) && isset($item['id']) && is_int($item['id']) && $item['id'] > $after) {
            echo json_encode(['id' => $item['id']]) . "\n";
        }
    }
    flock($manifest, LOCK_UN);
    fclose($manifest);
}

function send_image(string $dataDirectory): void
{
    $id = isset($_GET['id']) ? filter_var($_GET['id'], FILTER_VALIDATE_INT) : false;
    if ($id === false || $id < 1) {
        fail(404, 'Not found.');
    }
    $path = sprintf('%s/%08d.jpg', $dataDirectory, $id);
    if (!is_file($path)) {
        fail(404, 'Not found.');
    }
    header('Content-Type: image/jpeg');
    header('Content-Length: ' . filesize($path));
    header('Cache-Control: private, no-store');
    header('X-Content-Type-Options: nosniff');
    readfile($path);
}

[$roomToken, $dataDirectory] = configuration();
require_token($roomToken);
$action = isset($_GET['action']) && is_string($_GET['action']) ? $_GET['action'] : 'page';

if ($action === 'upload' && $_SERVER['REQUEST_METHOD'] === 'POST') {
    upload_image($dataDirectory);
    exit;
}
if ($action === 'feed' && $_SERVER['REQUEST_METHOD'] === 'GET') {
    send_feed($dataDirectory);
    exit;
}
if ($action === 'image' && $_SERVER['REQUEST_METHOD'] === 'GET') {
    send_image($dataDirectory);
    exit;
}
if ($action !== 'page' || $_SERVER['REQUEST_METHOD'] !== 'GET') {
    fail(405, 'Method not allowed.');
}

$nonce = base64_encode(random_bytes(18));
header("Content-Security-Policy: default-src 'none'; img-src blob:; style-src 'nonce-$nonce'; script-src 'nonce-$nonce'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'");
header('Referrer-Policy: no-referrer');
header('X-Content-Type-Options: nosniff');
header('Cache-Control: no-store');
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Send a photo</title>
  <style nonce="<?= htmlspecialchars($nonce, ENT_QUOTES, 'UTF-8') ?>">
    :root { color-scheme: light; font-family: system-ui, sans-serif; background: #f4efe8; color: #17130f; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; }
    main { box-sizing: border-box; width: min(100%, 34rem); padding: 2rem 1.25rem; text-align: center; }
    h1 { margin: 0 0 .6rem; font-size: clamp(2rem, 10vw, 3.5rem); line-height: 1; }
    p { font-size: 1.1rem; line-height: 1.45; }
    .picker, button { display: block; box-sizing: border-box; width: 100%; border: 0; border-radius: .8rem; padding: 1rem; font: inherit; font-weight: 700; cursor: pointer; }
    .picker { margin-top: 1.5rem; background: #fff; border: 2px solid #17130f; }
    button { margin-top: .75rem; background: #17130f; color: #fff; }
    button:disabled { cursor: default; opacity: .4; }
    input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
    img { display: none; width: 100%; max-height: 50vh; margin-top: 1rem; border-radius: .8rem; object-fit: contain; background: #ddd5cb; }
    img.visible { display: block; }
    #status { min-height: 3.2rem; }
    #status.error { color: #a21919; font-weight: 650; }
    #status.success { color: #17662c; font-weight: 650; }
  </style>
</head>
<body>
<main>
  <h1 id="title">Send a photo</h1>
  <p id="introduction">Choose a photo to share during the show.</p>
  <label class="picker" for="photo" id="choose">Choose a photo</label>
  <input id="photo" type="file" accept="image/*">
  <img id="preview" alt="Selected photo">
  <button id="send" type="button" disabled>Send photo</button>
  <p id="status" role="status" aria-live="polite">No photo selected.</p>
</main>
<script nonce="<?= htmlspecialchars($nonce, ENT_QUOTES, 'UTF-8') ?>">
'use strict';

const messages = {
  en: {
    title: 'Send a photo',
    introduction: 'Choose a photo to share during the show.',
    choose: 'Choose a photo',
    preview: 'Selected photo',
    send: 'Send photo',
    empty: 'No photo selected.',
    preparing: 'Preparing your photo…',
    ready: 'Your photo is ready to send.',
    sending: 'Sending your photo…',
    sent: 'Your photo was sent!',
    heic: 'Neither this webpage nor your browser can read this high efficiency HEIC photo, ask Tom for this feature!',
    unreadable: 'This photo cannot be read. Please choose another one.',
    failed: 'The photo could not be sent. Please try again.'
  },
  fr: {
    title: 'Envoyez une photo',
    introduction: 'Choisissez une photo à partager pendant le spectacle.',
    choose: 'Choisir une photo',
    preview: 'Photo sélectionnée',
    send: 'Envoyer la photo',
    empty: 'Aucune photo sélectionnée.',
    preparing: 'Préparation de votre photo…',
    ready: 'Votre photo est prête à être envoyée.',
    sending: 'Envoi de votre photo…',
    sent: 'Votre photo a été envoyée !',
    heic: 'Ni cette page web ni votre navigateur ne peuvent lire cette photo HEIC haute efficacité. Demandez à Tom d’ajouter cette fonctionnalité !',
    unreadable: 'Cette photo ne peut pas être lue. Choisissez-en une autre.',
    failed: 'La photo n’a pas pu être envoyée. Veuillez réessayer.'
  }
};

const language = navigator.language.toLowerCase().startsWith('fr') ? 'fr' : 'en';
const text = messages[language];
const input = document.querySelector('#photo');
const preview = document.querySelector('#preview');
const send = document.querySelector('#send');
const status = document.querySelector('#status');
let prepared = null;
let previewUrl = null;
let selection = 0;

document.documentElement.lang = language;
document.title = text.title;
for (const id of ['title', 'introduction', 'choose', 'send']) {
  document.querySelector(`#${id}`).textContent = text[id];
}
preview.alt = text.preview;
status.textContent = text.empty;

input.addEventListener('change', async () => {
  const currentSelection = ++selection;
  prepared = null;
  send.disabled = true;
  preview.classList.remove('visible');
  if (previewUrl) {
    URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  }
  setStatus(text.preparing);
  const file = input.files[0];
  if (!file) {
    setStatus(text.empty);
    return;
  }
  try {
    const converted = await preparePhoto(file);
    if (currentSelection !== selection) return;
    prepared = converted;
    previewUrl = URL.createObjectURL(prepared);
    preview.src = previewUrl;
    preview.classList.add('visible');
    send.disabled = false;
    setStatus(text.ready);
  } catch (error) {
    if (currentSelection !== selection) return;
    setStatus(isHeic(file) ? text.heic : text.unreadable, 'error');
  }
});

send.addEventListener('click', async () => {
  if (!prepared) return;
  send.disabled = true;
  setStatus(text.sending);
  const body = new FormData();
  body.append('photo', prepared, 'photo.jpg');
  const url = new URL(window.location.href);
  url.searchParams.set('action', 'upload');
  try {
    const response = await fetch(url, { method: 'POST', body });
    if (!response.ok) throw new Error('upload failed');
    setStatus(text.sent, 'success');
  } catch (error) {
    send.disabled = false;
    setStatus(text.failed, 'error');
  }
});

function setStatus(message, style = '') {
  status.textContent = message;
  status.className = style;
}

function isHeic(file) {
  return ['image/heic', 'image/heif', 'image/vnd.android.heic'].includes(file.type.toLowerCase()) || /\.hei[cf]$/i.test(file.name);
}

async function preparePhoto(file) {
  const image = await loadImage(file);
  const scale = Math.min(1, 2048 / Math.max(image.naturalWidth, image.naturalHeight));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext('2d');
  if (!context) throw new Error('canvas unavailable');
  context.fillStyle = '#fff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) => {
    canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('JPEG encoding failed')), 'image/jpeg', 0.85);
  });
}

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(file);
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('image decoding failed'));
    };
    image.src = url;
  });
}
</script>
</body>
</html>
