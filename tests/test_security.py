from jenkins_k8s_mcp.security import sanitize


def test_named_secret_values_are_masked_without_removing_nonsensitive_configuration():
    data = {
        "env": [{"name": "DB_PASSWORD", "value": "private"}, {"name": "MODE", "value": "safe"}],
        "parameterDefinitions": [
            {
                "name": "Entry",
                "type": "PasswordParameterDefinition",
                "defaultParameterValue": {"value": "private"},
            }
        ],
    }
    result = sanitize(data)
    assert result["env"][0]["value"] == "[REDACTED]"
    assert result["env"][1]["value"] == "safe"
    assert result["parameterDefinitions"][0]["defaultParameterValue"] == "[REDACTED]"
    assert data["env"][0]["value"] == "private"
