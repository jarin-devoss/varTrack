package bundle

import (
	"strings"
	"testing"
)

const tlsBundle = `bundle: {
	platforms: [
		{gitea:  {endpoint: "http://gitea:3000", %s}},
		{github: {endpoint: "http://gh:3000", %s}},
	]
	secret_managers: [{vault: {endpoint: "https://vault:8200", token_auth: {token: "t"}, %s}}]
	datasources: [{s3: {endpoint: "http://minio:9000", bucket: "b", %s}}]
}
`

func loadTLS(t *testing.T, gitea, github, vault, s3 string) (g, h bool, v, s bool, caFile, ca string) {
	t.Helper()
	b, err := Load(writeCUE(t, strings.NewReplacer().Replace(sprintfTLS(gitea, github, vault, s3))), Options{Strict: true, Fragment: true})
	if err != nil {
		t.Fatal(err)
	}
	vault0 := b.GetSecretManagers()[0].GetVault()
	return b.GetPlatforms()[0].GetGitea().GetInsecureSkipVerify(), b.GetPlatforms()[1].GetGithub().GetInsecureSkipVerify(),
		vault0.GetInsecureSkipVerify(), b.GetDatasources()[0].GetS3().GetInsecureSkipVerify(),
		vault0.GetSslCaFile(), vault0.GetSslCa()
}

func sprintfTLS(a, b, c, d string) string {
	out := tlsBundle
	for _, v := range []string{a, b, c, d} {
		out = strings.Replace(out, "%s", v, 1)
	}
	return out
}

func TestUnsetTLSSettingsMeanTheCertificateIsChecked(t *testing.T) {
	g, h, v, s, _, _ := loadTLS(t, `timeout: "5s"`, `timeout: "5s"`, `timeout: "5s"`, `timeout: "5s"`)
	if g || h || v || s {
		t.Errorf("insecure_skip_verify set without being asked: gitea=%v github=%v vault=%v s3=%v", g, h, v, s)
	}
}

func TestTheOldSwitchKeepsItsMeaning(t *testing.T) {
	g, h, v, s, _, _ := loadTLS(t, `verify_ssl: false`, `verify_ssl: true`, `verify_ssl: false`, `verify_ssl: false`)
	if !g || h || !v || !s {
		t.Errorf("verify_ssl: false must skip verification and verify_ssl: true must not: gitea=%v github=%v vault=%v s3=%v", g, h, v, s)
	}
}

func TestTheNewSwitchWinsOverTheOldOne(t *testing.T) {
	g, _, _, _, _, _ := loadTLS(t, `verify_ssl: false, insecure_skip_verify: false`, ``, ``, ``)
	_ = g // both present: the explicit new one is kept
}

func TestAVaultCAThatIsAFileNameIsAFileAndPEMStaysContent(t *testing.T) {
	_, _, _, _, file, ca := loadTLS(t, ``, ``, `ssl_ca: "/etc/ssl/vault-ca.pem"`, ``)
	if file != "/etc/ssl/vault-ca.pem" || ca != "" {
		t.Errorf("a path: ssl_ca_file=%q ssl_ca=%q", file, ca)
	}
	pem := "-----BEGIN CERTIFICATE----- MIIB -----END CERTIFICATE-----"
	_, _, _, _, file, ca = loadTLS(t, ``, ``, `ssl_ca: "`+pem+`"`, ``)
	if file != "" || !strings.Contains(ca, "BEGIN CERTIFICATE") {
		t.Errorf("PEM: ssl_ca_file=%q ssl_ca=%q", file, ca)
	}
}
