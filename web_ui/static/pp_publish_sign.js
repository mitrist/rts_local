/**
 * PP publish flow: prepare job → CryptoPro Browser Plugin → submit signatures.
 * Requires cadesplugin (window.cadesplugin) from CryptoPro extension/plugin.
 */
(function (global) {
  "use strict";

  var CADESCOM_CADES_BES = 1;
  var CADESCOM_BASE64_TO_BINARY = 1;
  var CAPICOM_CURRENT_USER_STORE = 2;
  var CAPICOM_MY_STORE = "My";
  var CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED = 2;
  var CAPICOM_CERTIFICATE_FIND_TIME_VALID = 9;
  var CAPICOM_CERTIFICATE_FIND_EXTENDED_PROPERTY = 6;
  var CAPICOM_CERT_INFO_SUBJECT_SIMPLE_NAME = 0;
  var CAPICOM_AUTHENTICATED_ATTRIBUTE_SIGNING_TIME = 0;
  var CADESCOM_AUTHENTICATED_ATTRIBUTE_DOCUMENT_NAME = 1;

  var selectedCertThumbprint = null;

  function statusLabels(status) {
    var map = {
      preparing: "Готовим к публикации…",
      awaiting_signature: "Подпишите ЭП (КриптоПро)",
      timestamping: "Метка времени…",
      publishing: "Публикуем в РТС…",
      published: "Опубликовано",
      failed: "Ошибка публикации",
      cancelled: "Отменено"
    };
    return map[status] || status || "";
  }

  function fetchJson(url, options) {
    options = options || {};
    options.credentials = options.credentials || "same-origin";
    options.headers = options.headers || {};
    options.headers.Accept = "application/json";
    if (options.body && !options.headers["Content-Type"]) {
      options.headers["Content-Type"] = "application/json";
    }
    return fetch(url, options).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        if (text) {
          try {
            data = JSON.parse(text);
          } catch (e) {
            /* ignore */
          }
        }
        if (!resp.ok) {
          var detail = data && data.detail ? data.detail : (text || ("HTTP " + resp.status));
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        if (!data) {
          throw new Error(text || "Пустой ответ сервера");
        }
        return data;
      });
    });
  }

  function ensureCadesPlugin() {
    if (!global.cadesplugin) {
      return Promise.reject(
        new Error(
          "Не найден плагин КриптоПро Browser Plugin. Установите его и обновите страницу."
        )
      );
    }
    return global.cadesplugin;
  }

  function canAsync() {
    return !!(global.cadesplugin && global.cadesplugin.CreateObjectAsync);
  }

  function pickCertificateAsync() {
    return ensureCadesPlugin().then(function () {
      return global.cadesplugin.CreateObjectAsync("CAdESCOM.Store").then(function (store) {
        return store.Open(CAPICOM_CURRENT_USER_STORE, CAPICOM_MY_STORE, CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED)
          .then(function () {
            return store.Certificates;
          })
          .then(function (certs) {
            return certs.Find(CAPICOM_CERTIFICATE_FIND_TIME_VALID);
          })
          .then(function (validCerts) {
            return validCerts.Find(CAPICOM_CERTIFICATE_FIND_EXTENDED_PROPERTY, 2 /* CAPICOM_PROPID_KEY_PROV_INFO */)
              .then(function (certsWithKey) {
                return certsWithKey.Count.then(function (count) {
                  if (!count || count < 1) {
                    throw new Error("Нет подходящих сертификатов ЭП с закрытым ключом.");
                  }
                  var promises = [];
                  for (var i = 1; i <= count; i++) {
                    (function (idx) {
                      promises.push(
                        certsWithKey.Item(idx).then(function (cert) {
                          return Promise.all([
                            cert.Thumbprint,
                            cert.GetInfo(CAPICOM_CERT_INFO_SUBJECT_SIMPLE_NAME),
                            cert.ValidToDate
                          ]).then(function (parts) {
                            return {
                              cert: cert,
                              thumbprint: parts[0],
                              subject: parts[1],
                              validTo: parts[2]
                            };
                          });
                        })
                      );
                    })(i);
                  }
                  return Promise.all(promises);
                });
              });
          })
          .then(function (list) {
            store.Close();
            if (selectedCertThumbprint) {
              for (var j = 0; j < list.length; j++) {
                if (String(list[j].thumbprint).toUpperCase() === String(selectedCertThumbprint).toUpperCase()) {
                  return list[j].cert;
                }
              }
            }
            if (list.length === 1) {
              selectedCertThumbprint = list[0].thumbprint;
              return list[0].cert;
            }
            var lines = list.map(function (c, idx) {
              return (idx + 1) + ") " + c.subject + " [" + c.thumbprint + "]";
            });
            var choice = global.prompt(
              "Выберите сертификат (номер):\n" + lines.join("\n"),
              "1"
            );
            var n = parseInt(choice, 10);
            if (!n || n < 1 || n > list.length) {
              throw new Error("Сертификат не выбран.");
            }
            selectedCertThumbprint = list[n - 1].thumbprint;
            return list[n - 1].cert;
          });
      });
    });
  }

  function hexToBase64(hex) {
    var h = String(hex || "").replace(/\s+/g, "");
    if (h.length % 2) h = "0" + h;
    var bytes = [];
    for (var i = 0; i < h.length; i += 2) {
      bytes.push(parseInt(h.substr(i, 2), 16));
    }
    var bin = "";
    for (var j = 0; j < bytes.length; j++) {
      bin += String.fromCharCode(bytes[j]);
    }
    return global.btoa(bin);
  }

  function signOneAsync(cert, req) {
    return global.cadesplugin.CreateObjectAsync("CAdESCOM.CPSigner").then(function (signer) {
      return signer.propset_Certificate(cert).then(function () {
        return global.cadesplugin.CreateObjectAsync("CAdESCOM.CadesSignedData").then(function (signedData) {
          var encoding = req.encoding === "hex_hash" ? "hex_hash" : "base64";
          var chain;
          if (encoding === "hex_hash") {
            chain = global.cadesplugin.CreateObjectAsync("CAdESCOM.HashedData").then(function (hashed) {
              return hashed.propset_Algorithm(4 /* CADESCOM_HASH_ALGORITHM_SHA_256 */)
                .then(function () {
                  // Prefer SetHashValue with Base64 of raw hash bytes (SHA-256 from RTS).
                  var b64 = hexToBase64(req.data);
                  return hashed.SetHashValue(b64).then(function () {
                    return signedData.SignHash(hashed, signer, CADESCOM_CADES_BES);
                  });
                })
                .catch(function () {
                  // Fallback: treat hash hex as content (some plugin builds)
                  return signedData.propset_ContentEncoding(CADESCOM_BASE64_TO_BINARY).then(function () {
                    return signedData.propset_Content(hexToBase64(req.data)).then(function () {
                      return signedData.SignCades(signer, CADESCOM_CADES_BES, true);
                    });
                  });
                });
            });
          } else {
            chain = signedData.propset_ContentEncoding(CADESCOM_BASE64_TO_BINARY).then(function () {
              return signedData.propset_Content(req.data).then(function () {
                return signedData.SignCades(signer, CADESCOM_CADES_BES, true);
              });
            });
          }
          return chain.then(function (cms) {
            return {
              fileGuid: req.file_guid == null ? null : req.file_guid,
              signatureWithTimeStamp: String(cms || "").replace(/\s+/g, "")
            };
          });
        });
      });
    });
  }

  function signRequests(signRequests) {
    if (!canAsync()) {
      return Promise.reject(
        new Error("Нужен КриптоПро Browser Plugin с поддержкой CreateObjectAsync (Chrome/Yandex/Edge).")
      );
    }
    return pickCertificateAsync().then(function (cert) {
      var seq = Promise.resolve([]);
      (signRequests || []).forEach(function (req) {
        seq = seq.then(function (acc) {
          return signOneAsync(cert, req).then(function (sig) {
            acc.push(sig);
            return acc;
          });
        });
      });
      return seq;
    });
  }

  /**
   * @param {object} opts
   * @param {number|string} opts.rtsDraftId
   * @param {string} [opts.source]
   * @param {string} [opts.sourceKey]
   * @param {boolean} [opts.forceNew]
   * @param {function(string,string=)} opts.onStatus  (message, kind)
   * @param {HTMLElement} [opts.statusEl]
   */
  function runPublishFlow(opts) {
    opts = opts || {};
    var draftId = opts.rtsDraftId;
    var onStatus = opts.onStatus || function () {};
    var set = function (msg, kind) {
      onStatus(msg, kind || "");
      if (opts.statusEl) {
        opts.statusEl.hidden = false;
        opts.statusEl.textContent = msg;
        opts.statusEl.className = "offer-participate-status" +
          (kind ? " offer-participate-status--" + kind : "");
      }
    };

    if (!draftId) {
      set("Нет id черновика для публикации.", "error");
      return Promise.reject(new Error("no draft id"));
    }

    set("Готовим к публикации…", "pending");
    return fetchJson("/api/rts/pp/publish/start", {
      method: "POST",
      body: JSON.stringify({
        rts_draft_id: Number(draftId),
        source: opts.source || "",
        source_key: opts.sourceKey || "",
        force_new: !!opts.forceNew
      })
    }).then(function (job) {
      if (job.status === "published") {
        set("Уже опубликовано (job " + job.job_id + ").", "ok");
        return job;
      }
      if (job.status !== "awaiting_signature") {
        throw new Error(job.error || ("Неожиданный статус: " + job.status));
      }
      set(statusLabels("awaiting_signature") + "…", "pending");
      return signRequests(job.sign_requests || []).then(function (signatures) {
        set("Публикуем в РТС…", "pending");
        return fetchJson("/api/rts/pp/publish/submit-signatures", {
          method: "POST",
          body: JSON.stringify({
            job_id: job.job_id,
            signatures: signatures
          })
        });
      });
    }).then(function (finalJob) {
      if (finalJob.status === "published") {
        set("Опубликовано. Черновик id " + (finalJob.rts_draft_id || draftId) + ".", "ok");
      } else {
        set(finalJob.error || statusLabels(finalJob.status), finalJob.ok ? "ok" : "error");
      }
      return finalJob;
    }).catch(function (err) {
      var msg = err && err.message ? err.message : String(err);
      if (/404|отключена|disabled/i.test(msg)) {
        msg = "Публикация отключена на сервере (RTS_PP_PUBLISH_ENABLED).";
      }
      set(msg, "error");
      throw err;
    });
  }

  global.RtsPpPublish = {
    run: runPublishFlow,
    statusLabels: statusLabels,
    signRequests: signRequests
  };
})(window);
