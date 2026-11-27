"""What: ingress step 13, structured input (doc 07 s2.4 step 13, s6.3, s7.3): grammar (strict yes and no),
pending (current pending, adjacency, card), verification (the masked card body), confirm (the ONLY user of the
commit token: commit, submit, cancel, status check) and handler (the step 13 order).
Use when: import the submodule you need; this package file imports nothing, so importing the package never
loads the commit client.
Inputs: none. Output: none. Side effects: none.
Role: package marker. Tool modules must never import agent.structured.confirm (CI import guard, doc 07 s1.4).
"""
