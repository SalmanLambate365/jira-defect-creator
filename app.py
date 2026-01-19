
# --- Link defect to Zephyr execution ---
try:
    execution_obj = find_latest_execution(test_ticket.strip(), auth)

    if not execution_obj:
        st.warning("No Zephyr execution found for this Test.")
    else:
        try:
            link_defect_to_execution_cloud(execution_obj, issue_key, auth)
            st.success("🔗 Defect linked to Zephyr execution.")
        except Exception as e:
            st.warning(f"Failed to link defect to Zephyr execution: {e}")

except Exception as e:
    st.warning(f"Zephyr operations failed: {e}")

# --- (Optional) Update step result in Zephyr ---
try:
    if execution_obj and ENABLE_STEP_UPDATE:
        fail_zephyr_step(execution_obj.get('id'), failed_step_num)
        st.success("❗ Failed step updated in Zephyr.")
    elif execution_obj:
        st.info("Step update skipped (ENABLE_STEP_UPDATE = False).")
except Exception as e:
    st.warning(f"Could not update Zephyr failed step: {e}")

# --- Set overall execution status to FAIL in Zephyr ---
try:
    if execution_obj:
        fail_execution_cloud(execution_obj)
        st.success("🟥 Zephyr execution status set to FAIL (Zephyr).")
except Exception as e:
    st.warning(f"Failed to update Zephyr execution status: {e}")

# --- Safe link rendering (works across reruns) ---
ik = st.session_state.get("issue_key")
if ik:
    st.link_button("Open Defect in Jira", f"{JIRA_BASE_URL}/browse/{ik}")
else:
    st.info("Create a defect first to enable the Jira link.")


import hashlib
import hmac
