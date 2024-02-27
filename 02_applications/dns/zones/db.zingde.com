$ORIGIN zingde.com.
$TTL    6h

@       SOA     ns1.reisman.org. hostmaster.zingde.com. (
                2022042402      ; serial
                3h              ; refresh
                15m             ; retry
                4w              ; expire
                15m             ; negttl
                )

        NS      ns1.reisman.org.
        NS      ns2.reisman.org.

@       A       216.165.140.134

www     A       216.165.140.134

mail    A       216.165.140.134

; Mail
@       MX      10 mail.zingde.com.
        TXT     "v=spf1 a:mail.zingde.com -all"

20220424._domainkey     TXT     ( "v=DKIM1; h=sha256; k=rsa; p="
                        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA41x+vCzqqv2u2p6YTRjv"
                        "W3xnl4pnqG095wjg52y0KE6QsqQFlNpOYmWVJ7HZceF1yxHh+IxXCqFkwAKhgKqM"
                        "jvMlfg8N8FeR6bypIZ878dQ7huPAC63t7rrFUELGNJwSSa+DDpN7BMLCvdsQw2s1"
                        "W955vU8DL++qIfttob1WdzM2TMMG9/kpPaQIduMupiL8Rwylg8ndQasmyQlmiW/o"
                        "9Ni7EmROVM/rRUqbu6NilquiNg/WQZ39xRocHGSeWR1s0OYmfWMrsIA9hVsvrJsl"
                        "zM85JdoXH6/UBzJmXONehBgVFdsw9ItOa65VP0TquRDeMtWizYwMBzTPAAQFmzbq"
                        "cQIDAQAB" )  ; ----- DKIM key 20220424 for zingde.com

_dmarc  TXT     "v=DMARC1; p=reject; rua=mailto:dmarc-rua@zingde.com; ruf=mailto:dmarc-ruf@zingde.com; sp=reject; fo=1;"
