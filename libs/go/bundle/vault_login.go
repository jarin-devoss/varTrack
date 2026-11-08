package bundle

import (
	"fmt"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
	pb_utils "vartrack/libs/gen/vartrack/v1/utils"
)

// checkVaultLogins refuses a Vault secret manager whose own login credential (token_auth.token,
// app_role_auth.secret_id, userpass_auth.password) is a {ref:}: Vault would have to read it from itself
// before it could log in. A string or a {file:} (a mounted secret) is what these take.
func checkVaultLogins(bundle *pb_models.Bundle) error {
	for i, sm := range bundle.GetSecretManagers() {
		v := sm.GetVault()
		if v == nil {
			continue
		}
		for field, ref := range map[string]*pb_utils.SecretRef{
			"token_auth.token":        v.GetTokenAuth().GetToken(),
			"app_role_auth.secret_id": v.GetAppRoleAuth().GetSecretId(),
			"userpass_auth.password":  v.GetUserpassAuth().GetPassword(),
		} {
			if _, isRef := ref.GetSource().(*pb_utils.SecretRef_Ref); isRef {
				return fmt.Errorf("secret_managers[%d] (vault): %s cannot be a {ref:}: Vault would have to read "+
					"it from itself before it could log in; use a string or {file: …}", i, field)
			}
		}
	}
	return nil
}
