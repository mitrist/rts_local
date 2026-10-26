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
  var CAPICOM_CERTIFICATE_INCLUDE_END_ENTITY_ONLY = 2;
  // Match RTS CryptoProBrowserPluginProviderAsync.js
  var CADESCOM_HASH_ALGORITHM_SHA1 = 0;
  var CADESCOM_HASH_ALGORITHM_CP_GOST_3411 = 100;
  var CADESCOM_HASH_ALGORITHM_CP_GOST_3411_2012_256 = 101;
  var CADESCOM_HASH_ALGORITHM_CP_GOST_3411_2012_512 = 102;
  var OID_CP_GOST_R3410_12_256 = "1.2.643.7.1.1.1.1";
  var OID_CP_GOST_R3410_12_512 = "1.2.643.7.1.1.1.2";
  var OID_CP_RSA = "1.2.840.113549.1.1.1";
  var OID_CP_STB_1179_99 = "1.3.6.1.4.1.12656.1.37";
  var OID_CP_STB_34_101_45 = "1.2.112.0.2.0.34.101.45.2.1";

  var CERT_STORAGE_KEY = "rtsPpCertThumbprint";
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

  function normalizeThumbprint(value) {
    return String(value || "")
      .replace(/\s+/g, "")
      .toUpperCase();
  }

  function loadSavedThumbprint() {
    if (selectedCertThumbprint) {
      return normalizeThumbprint(selectedCertThumbprint);
    }
    try {
      if (global.localStorage) {
        var raw = global.localStorage.getItem(CERT_STORAGE_KEY);
        if (raw) {
          selectedCertThumbprint = raw;
          return normalizeThumbprint(raw);
        }
      }
    } catch (e) {
      /* private mode / blocked storage */
    }
    return "";
  }

  function saveThumbprint(thumbprint) {
    var norm = normalizeThumbprint(thumbprint);
    if (!norm) return;
    selectedCertThumbprint = norm;
    try {
      if (global.localStorage) {
        global.localStorage.setItem(CERT_STORAGE_KEY, norm);
      }
    } catch (e) {
      /* ignore */
    }
  }

  function clearSavedThumbprint() {
    selectedCertThumbprint = null;
    try {
      if (global.localStorage) {
        global.localStorage.removeItem(CERT_STORAGE_KEY);
      }
    } catch (e) {
      /* ignore */
    }
  }

  function findCertByThumbprint(list, thumbprint) {
    var want = normalizeThumbprint(thumbprint);
    if (!want) return null;
    for (var j = 0; j < list.length; j++) {
      if (normalizeThumbprint(list[j].thumbprint) === want) {
        return list[j];
      }
    }
    return null;
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
            var saved = loadSavedThumbprint();
            var remembered = findCertByThumbprint(list, saved);
            if (remembered) {
              saveThumbprint(remembered.thumbprint);
              return remembered.cert;
            }
            if (list.length === 1) {
              saveThumbprint(list[0].thumbprint);
              return list[0].cert;
            }
            var lines = list.map(function (c, idx) {
              return (idx + 1) + ") " + c.subject + " [" + c.thumbprint + "]";
            });
            var choice = global.prompt(
              "Выберите сертификат (номер).\n" +
                "Выбор сохранится в браузере — дальше спрашивать не будем.\n\n" +
                lines.join("\n"),
              "1"
            );
            var n = parseInt(choice, 10);
            if (!n || n < 1 || n > list.length) {
              throw new Error("Сертификат не выбран.");
            }
            saveThumbprint(list[n - 1].thumbprint);
            return list[n - 1].cert;
          });
      });
    });
  }

  function normalizeHexHash(hex) {
    return String(hex || "").replace(/\s+/g, "").toUpperCase();
  }

  function isRsaOrForeignOid(oid) {
    return (
      oid === OID_CP_RSA ||
      oid === OID_CP_STB_1179_99 ||
      oid === OID_CP_STB_34_101_45
    );
  }

  function hashAlgorithmForCertOid(oid) {
    if (oid === OID_CP_GOST_R3410_12_256) {
      return CADESCOM_HASH_ALGORITHM_CP_GOST_3411_2012_256;
    }
    if (oid === OID_CP_GOST_R3410_12_512) {
      return CADESCOM_HASH_ALGORITHM_CP_GOST_3411_2012_512;
    }
    if (isRsaOrForeignOid(oid)) {
      return CADESCOM_HASH_ALGORITHM_SHA1;
    }
    // Legacy GOST R 34.10-2001 and unknown GOST → GOST R 34.11-94
    return CADESCOM_HASH_ALGORITHM_CP_GOST_3411;
  }

  function getCertAlgorithmOid(cert) {
    return cert.PublicKey().then(function (pk) {
      return pk.Algorithm.then(function (alg) {
        return alg.Value;
      });
    });
  }

  function configureSigner(signer, cert) {
    return signer.propset_Certificate(cert).then(function () {
      // Same as RTS LK provider — end-entity only keeps CMS compact and matches verify.
      if (typeof signer.propset_Options === "function") {
        return signer.propset_Options(CAPICOM_CERTIFICATE_INCLUDE_END_ENTITY_ONLY);
      }
      return undefined;
    });
  }

  function signOneAsync(cert, req) {
    return global.cadesplugin.CreateObjectAsync("CAdESCOM.CPSigner").then(function (signer) {
      return configureSigner(signer, cert).then(function () {
        return global.cadesplugin.CreateObjectAsync("CAdESCOM.CadesSignedData").then(function (signedData) {
          var encoding = req.encoding === "hex_hash" ? "hex_hash" : "base64";
          var chain;
          if (encoding === "hex_hash") {
            var hexHash = normalizeHexHash(req.data);
            if (!/^[0-9A-F]+$/.test(hexHash) || hexHash.length % 2) {
              return Promise.reject(new Error("Некорректный hash файла для подписи."));
            }
            chain = getCertAlgorithmOid(cert).then(function (oid) {
              var algo = hashAlgorithmForCertOid(oid);
              return global.cadesplugin.CreateObjectAsync("CAdESCOM.HashedData").then(function (hashed) {
                return hashed.propset_Algorithm(algo).then(function () {
                  // RTS passes hex string to SetHashValue (not Base64).
                  return hashed.SetHashValue(hexHash).then(function () {
                    return signedData.SignHash(hashed, signer, CADESCOM_CADES_BES);
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

  function signKey(req) {
    return String((req && req.encoding) || "") + "\n" + String((req && req.data) || "");
  }

  function signRequests(signRequests, onProgress) {
    if (!canAsync()) {
      return Promise.reject(
        new Error("Нужен КриптоПро Browser Plugin с поддержкой CreateObjectAsync (Chrome/Yandex/Edge).")
      );
    }
    var reqs = signRequests || [];
    return pickCertificateAsync().then(function (cert) {
      var cache = {};
      var seq = Promise.resolve([]);
      var uniqueDone = 0;
      var uniqueTotal = 0;
      var seen = {};
      reqs.forEach(function (req) {
        var key = signKey(req);
        if (!seen[key]) {
          seen[key] = true;
          uniqueTotal += 1;
        }
      });
      reqs.forEach(function (req, idx) {
        seq = seq.then(function (acc) {
          var key = signKey(req);
          var guid = req.file_guid == null ? null : req.file_guid;
          if (cache[key]) {
            acc.push({
              fileGuid: guid,
              signatureWithTimeStamp: cache[key]
            });
            if (typeof onProgress === "function") {
              onProgress({
                index: idx + 1,
                total: reqs.length,
                uniqueDone: uniqueDone,
                uniqueTotal: uniqueTotal,
                reused: true
              });
            }
            return acc;
          }
          if (typeof onProgress === "function") {
            onProgress({
              index: idx + 1,
              total: reqs.length,
              uniqueDone: uniqueDone + 1,
              uniqueTotal: uniqueTotal,
              reused: false
            });
          }
          return signOneAsync(cert, req).then(function (sig) {
            cache[key] = sig.signatureWithTimeStamp;
            uniqueDone += 1;
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
      var reqs = job.sign_requests || [];
      return signRequests(reqs, function (p) {
        if (!p || p.reused) return;
        set(
          "КриптоПро: подпись " + p.uniqueDone + " из " + p.uniqueTotal +
            " (слотов " + p.index + "/" + p.total + ")…",
          "pending"
        );
      }).then(function (signatures) {
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
      if (/RTS_PP_PUBLISH_ENABLED|Публикация ПП отключена/i.test(msg)) {
        msg = "Публикация отключена на сервере (RTS_PP_PUBLISH_ENABLED).";
      }
      set(msg, "error");
      throw err;
    });
  }

  global.RtsPpPublish = {
    run: runPublishFlow,
    statusLabels: statusLabels,
    signRequests: signRequests,
    watchPending: watchPendingSignatures,
    clearPreferredCert: clearSavedThumbprint,
    getPreferredCertThumbprint: loadSavedThumbprint
  };

  var _pendingWatchStarted = false;
  var _pendingBusy = false;
  var _promptedJobIds = {};

  function watchPendingSignatures(opts) {
    opts = opts || {};
    if (_pendingWatchStarted) return;
    _pendingWatchStarted = true;
    var intervalMs = opts.intervalMs || 20000;
    var runOnce = function () {
      if (_pendingBusy) return;
      if (document.hidden) return;
      fetchJson("/api/rts/pp/publish/pending-signatures", { method: "GET" })
        .then(function (data) {
          if (!data || !data.enabled || !data.jobs || !data.jobs.length) return;
          var jobs = data.jobs.filter(function (j) {
            var id = String(j.job_id || "");
            return id && !_promptedJobIds[id];
          });
          if (!jobs.length) return;
          var autoJobs = jobs.filter(function (j) {
            return j.source === "auto_monitor" || j.source === "notice" || j.source === "core";
          });
          var pick = autoJobs.length ? autoJobs : jobs;
          // Silent when cert thumbprint already saved (first pick stores rtsPpCertThumbprint).
          var silent = !!loadSavedThumbprint();
          if (!silent) {
            var msg =
              "Есть " +
              pick.length +
              " ПП, ожидающих подписи ЭП для публикации.\n\n" +
              "Подписать сейчас через КриптоПро?\n" +
              "Сертификат сохранится — следующие подписи без этого вопроса.";
            if (!global.confirm(msg)) {
              pick.forEach(function (j) {
                _promptedJobIds[String(j.job_id)] = true;
              });
              return;
            }
          }
          _pendingBusy = true;
          var seq = Promise.resolve();
          pick.forEach(function (j) {
            var jobId = String(j.job_id);
            _promptedJobIds[jobId] = true;
            seq = seq.then(function () {
              return runPublishFlow({
                rtsDraftId: j.rts_draft_id,
                source: j.source || "",
                sourceKey: j.source_key || "",
                forceNew: false,
                onStatus: function (m) {
                  if (opts.onStatus) opts.onStatus(m);
                }
              }).catch(function (err) {
                if (silent) {
                  // Allow retry on next poll tick when auto-signing.
                  delete _promptedJobIds[jobId];
                  if (opts.onStatus) {
                    opts.onStatus(
                      "Публикация " +
                        (j.rts_draft_id || "") +
                        ": " +
                        (err && err.message ? err.message : String(err)),
                      "error"
                    );
                  }
                  return;
                }
                global.alert(
                  "Публикация черновика " +
                    (j.rts_draft_id || "") +
                    ": " +
                    (err && err.message ? err.message : String(err))
                );
              });
            });
          });
          return seq.then(function () {
            _pendingBusy = false;
          }, function () {
            _pendingBusy = false;
          });
        })
        .catch(function () {
          /* ignore poll errors */
        });
    };
    runOnce();
    global.setInterval(runOnce, intervalMs);
  }
})(window);
