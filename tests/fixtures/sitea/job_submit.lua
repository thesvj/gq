json = require("dkjson")
token_file = "/var/lib/slurm/tokens/group_tokens.json"
DEFAULT_TOKENS = 36.00
TOKEN_COST = {
    short  = 0.10,
    medium = 0.50,
    long   = 1.00,
}
function slurm_job_submit(job_desc, part_list, submit_uid) os.execute("never run") end
