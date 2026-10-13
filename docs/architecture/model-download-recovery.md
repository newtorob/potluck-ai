# Model download recovery

The downloader checks response headers before it changes the partial file, so a
server that ignores `Range` and sends the whole file with HTTP 200 cannot leave
duplicate bytes in a resumed download. Responses are handled as follows:

- **200:** truncate and restart, resetting downloaded bytes, total and speed tracking.
- **206:** require the requested start offset, valid bounds and consistent response
  length. Preserve received bytes on truncation or a short subrange for the next retry.
- **416:** if the server length equals the partial size, use the expected SHA256 to
  verify and promote the existing file. Otherwise retry once without `Range`.
- Request identity encoding and reject compressed responses so resume offsets refer
  to the bytes on disk. Reject body overruns before appending excess data.
- Cancellation preserves received data. A cancellation during connection setup
  does not truncate an existing partial when the server returns a full response.

The header checks follow the byte-range rules in
[RFC 9110, section 14.4](https://www.rfc-editor.org/rfc/rfc9110.html#section-14.4).
Catalog sizes remain estimates; actual response framing and the expected hash
determine completion. The install API continues to require a catalog SHA256.
For a 206 response with unknown total length, a valid hash can prove completion;
on hash mismatch the partial is retained because it may only be incomplete.

## Tests

`apps/desktop/python/tests/test_model_download_resume.py` covers valid and invalid range
responses, ignored ranges, short, disconnected and oversized bodies, complete and
invalid partials, the bounded fallback, cancellation and retry, content encoding
and stale partial metadata. Tests use temporary files and HTTPX mock streams.

This behavior covers download recovery only. It does not add automatic retries
for every network error or change the UI, the catalog integrity requirement or
model loading.
