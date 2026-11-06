# Корневой сертификат для MAX API2

`russian_trusted_root_ca.crt` — публичный **Russian Trusted Root CA** Минцифры,
действующий до 27 февраля 2032 года. Это корневой сертификат, не сертификат сервера
и не промежуточный Sub CA.

- Официальная страница: https://www.gosuslugi.ru/crt
- Официальный PEM: https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
- SHA-256 по DER:
  `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31`.
- Отпечаток и назначение сверены с
  [MAX Messenger skill, tls-trust.md](https://github.com/max-messenger/max-agents-skill/blob/22b2eb9dc9c80c153b2d99e29ecfaaf0bfe75693/references/tls-trust.md).

Официальный сервер загрузки недоступен из проверочной облачной среды. Публичная
копия взята из
[koenrh/russian-trusted-root-ca](https://github.com/koenrh/russian-trusted-root-ca/blob/2432a9a6a98990765ecd920eb3ed9c5f109f4ce4/root-ca_rsa-2022.pem).
Её DER-отпечаток совпадает с указанным выше; subject, issuer, срок и самоподпись
проверены OpenSSL 6 ноября 2026 года. Исходный сертификат не изменялся.

Проверка отпечатка вручную:

```sh
openssl x509 -in certs/russian_trusted_root_ca.crt -noout -subject -issuer -dates -fingerprint -sha256
openssl verify -CAfile certs/russian_trusted_root_ca.crt certs/russian_trusted_root_ca.crt
```

`max_tls.py` также проверяет отпечаток перед созданием HTTPS-клиента MAX.
Сертификат добавляется к обычным доверенным CA HTTPX только в контексте этого
клиента. Проверка цепочки и имени сервера остаётся включённой. Telegram, LLM,
загрузка/скачивание файлов и системное хранилище доверия не изменяются.

При будущей замене корня сначала проверьте актуальные источники MAX/Госуслуг и
отпечаток нового сертификата; затем измените PEM, закреплённый SHA-256 и тесты
вместе. Отсутствующий или изменённый файл вызывает ошибку, а не отключение TLS.
