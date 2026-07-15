with open("ci_owner_agent/server.py", "r", encoding="utf-8") as f:
    c = f.read()

# Replace the old isActive query with FeedbackStore.list_feedback
old_query = '''        feedback_docs = list(store.feedback.find({"repo": repo, "job": job, "branch": branch, "buildNumber": build, "isActive": True}))'''

new_query = '''        feedback_docs = FeedbackStore(store).list_feedback(repo=repo, job=job, branch=branch, build_number=build)'''

if old_query in c:
    c = c.replace(old_query, new_query)
    print("Replaced isActive query with list_feedback")
else:
    print("FAIL: old isActive query not found")
    idx = c.find("isActive")
    if idx > 0:
        print(f"Found isActive at {idx}: {repr(c[idx-50:idx+100])}")

with open("ci_owner_agent/server.py", "w", encoding="utf-8", newline="\n") as f:
    f.write(c)
print("Done")