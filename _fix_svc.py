with open("ci_owner_agent/services/wecom_feedback_service.py", "r", encoding="utf-8") as f:
    c = f.read()

# 1. Remove the search_users block
old_block = '''            owner_name = intent.target_display_name
            owner_email = None
            if intent.target_userid:
                matches = self.users.search_users(intent.target_userid, limit=1)
                if matches and matches[0].get("wecomUserId") == intent.target_userid:
                    owner_name = matches[0].get("displayName") or owner_name
                    owner_email = matches[0].get("preferredEmail")'''

new_block = '''            owner_name = intent.target_display_name
            owner_email = None'''

if old_block in c:
    c = c.replace(old_block, new_block)
    print("Removed search_users block")
else:
    print("FAIL: search_users block not found")

# 2. Remove WeComUserDirectory import
old_import = "from ci_owner_agent.services.wecom_user_directory import WeComUserDirectory\n"
if old_import in c:
    c = c.replace(old_import, "")
    print("Removed WeComUserDirectory import")
else:
    print("FAIL: WeComUserDirectory import not found")

# 3. Remove self.users initialization
old_init = "        self.users = WeComUserDirectory(history_store)\n"
if old_init in c:
    c = c.replace(old_init, "")
    print("Removed self.users init")
else:
    print("FAIL: self.users init not found")

# 4. Check for any remaining references
if "self.users" in c:
    print("WARNING: self.users still referenced")
if "WeComUserDirectory" in c:
    print("WARNING: WeComUserDirectory still referenced")

with open("ci_owner_agent/services/wecom_feedback_service.py", "w", encoding="utf-8", newline="\n") as f:
    f.write(c)
print("Done")