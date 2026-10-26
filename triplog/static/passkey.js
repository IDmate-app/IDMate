// Passkeys (WebAuthn) for the login page and the profile.
// The server sends the options as JSON (base64url for binary fields); the
// browser API wants ArrayBuffers — converted here by hand, so it also works
// where PublicKeyCredential.parse*OptionsFromJSON is missing.
(function () {
  function b64uToBuf(s) {
    s = String(s).replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) s += '=';
    const bin = atob(s), out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }
  function bufToB64u(buf) {
    const b = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  const descs = list => (list || []).map(c => ({ ...c, id: b64uToBuf(c.id) }));

  async function api(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(j.error || ('HTTP ' + r.status)); e.data = j; e.status = r.status; throw e; }
    return j;
  }

  const Passkey = {
    supported() {
      return !!(window.PublicKeyCredential && navigator.credentials && window.isSecureContext);
    },
    async conditionalAvailable() {
      try {
        return !!(this.supported() && PublicKeyCredential.isConditionalMediationAvailable
                  && await PublicKeyCredential.isConditionalMediationAvailable());
      } catch (e) { return false; }
    },
    // Add a passkey for the signed-in user. extra: {password|code} when the
    // server asks for a fresh sign-in (error.data.reauth).
    async register(label, extra) {
      const b = await api('/api/passkeys/register/begin', extra);
      const pk = b.publicKey;
      const cred = await navigator.credentials.create({ publicKey: {
        ...pk,
        challenge: b64uToBuf(pk.challenge),
        user: { ...pk.user, id: b64uToBuf(pk.user.id) },
        excludeCredentials: descs(pk.excludeCredentials),
      } });
      const r = cred.response;
      return api('/api/passkeys/register/finish', { label, credential: {
        id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment || undefined,
        clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
        response: {
          clientDataJSON: bufToB64u(r.clientDataJSON),
          attestationObject: bufToB64u(r.attestationObject),
          transports: r.getTransports ? r.getTransports() : [],
        },
      } });
    },
    // Sign in. conditional: offer the passkeys in the user name autofill
    // (runs until a passkey is picked or `signal` aborts it).
    async login({ next, conditional, signal } = {}) {
      const b = await api('/login/passkey/begin');
      const pk = b.publicKey;
      const opts = { publicKey: { ...pk, challenge: b64uToBuf(pk.challenge),
                                  allowCredentials: descs(pk.allowCredentials) } };
      if (conditional) opts.mediation = 'conditional';
      if (signal) opts.signal = signal;
      const a = await navigator.credentials.get(opts);
      const r = a.response;
      const j = await api('/login/passkey/finish', { next, credential: {
        id: a.id, rawId: bufToB64u(a.rawId), type: a.type,
        authenticatorAttachment: a.authenticatorAttachment || undefined,
        clientExtensionResults: a.getClientExtensionResults ? a.getClientExtensionResults() : {},
        response: {
          clientDataJSON: bufToB64u(r.clientDataJSON),
          authenticatorData: bufToB64u(r.authenticatorData),
          signature: bufToB64u(r.signature),
          userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
        },
      } });
      location.href = j.next || '/';
      return j;
    },
    // Cancelled by the user or replaced by another request: no error message.
    isCancel(e) { return e && (e.name === 'AbortError' || e.name === 'NotAllowedError'); },
  };
  window.IDmatePasskey = Passkey;
})();
