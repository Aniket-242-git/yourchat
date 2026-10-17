/* ==========================================================================
 * Couple Chat — client-side E2E encryption (Web Crypto API only).
 *
 * Scheme: PBKDF2-SHA256 (210,000 iterations, random 16-byte salt)
 *         -> AES-256-GCM with a fresh random 12-byte IV per message.
 *
 * Only {salt, iv, ct} (base64) ever leave the browser. The password and the
 * plaintext are never sent or stored anywhere.
 * ========================================================================== */
(function (global) {
  "use strict";

  const PBKDF2_ITERATIONS = 210000;   // >= 200k as required
  const KEY_LENGTH_BITS = 256;
  const SALT_BYTES = 16;
  const IV_BYTES = 12;                // standard GCM nonce size

  // ---- base64 helpers -----------------------------------------------------
  function bufToB64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  function b64ToBuf(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  // ---- key derivation -------------------------------------------------------
  async function deriveKey(password, saltBytes) {
    const enc = new TextEncoder();
    const baseKey = await crypto.subtle.importKey(
      "raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]
    );
    return crypto.subtle.deriveKey(
      {
        name: "PBKDF2",
        salt: saltBytes,
        iterations: PBKDF2_ITERATIONS,
        hash: "SHA-256",
      },
      baseKey,
      { name: "AES-GCM", length: KEY_LENGTH_BITS },
      false,               // key never extractable
      ["encrypt", "decrypt"]
    );
  }

  /**
   * Encrypt `plaintext` with `password`.
   * Resolves to { salt, iv, ct } — all base64 strings, ready for JSON POST.
   */
  async function encryptMessage(plaintext, password) {
    if (!crypto.subtle) throw new Error("Web Crypto unavailable (needs HTTPS)");
    const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES));
    const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
    const key = await deriveKey(password, salt);
    const ct = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: iv },
      key,
      new TextEncoder().encode(plaintext)
    );
    return { salt: bufToB64(salt), iv: bufToB64(iv), ct: bufToB64(ct) };
  }

  /**
   * Decrypt {salt, iv, ct}. Rejects with Error("wrong_password") when the
   * GCM auth tag fails (i.e. wrong password or tampered ciphertext).
   */
  async function decryptMessage(pack, password) {
    const key = await deriveKey(password, new Uint8Array(b64ToBuf(pack.salt)));
    let plainBuf;
    try {
      plainBuf = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: new Uint8Array(b64ToBuf(pack.iv)) },
        key,
        b64ToBuf(pack.ct)
      );
    } catch (e) {
      throw new Error("wrong_password");
    }
    return new TextDecoder().decode(plainBuf);
  }

  global.CoupleCrypto = { encryptMessage, decryptMessage, PBKDF2_ITERATIONS };
})(window);
