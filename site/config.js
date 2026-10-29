// Where the backend lives.
//
// Leave this empty when server.py is serving these pages itself (http://localhost:8787/).
// When the pages are hosted somewhere else, like S3, set it to the backend's public address:
//   window.CONTEST_API = "https://your-tunnel-name.trycloudflare.com";
//   window.CONTEST_API = "http://192.168.1.20:8787";
// No trailing slash.
//
// A file named backend.json next to these pages overrides this, e.g. {"api": "https://abc.trycloudflare.com"}.
// That lets you point the pages at a new address by uploading one tiny file, with no edits here.
window.CONTEST_API = "";
