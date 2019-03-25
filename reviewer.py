import difflib
import enum
import re
import requests
import subprocess
import sys

class LineType(enum.Enum):
  GITDIFF = 'diff --git '
  INDEX = 'index '
  DELETED = 'deleted '
  ADDED = 'new file mode '
  FILE_OLD = '--- (?:a/)?(.*)'
  FILE_NEW = '\+\+\+ (?:b/)?(.*)'
  CHUNK = '@@ '
  DIFF = '[+-]'
  SIMILARITY = 'similarity index ([0-9]+)%'
  RENAME = 'rename (from|to) (.*)'
  CONTEXT = ' '
  EMPTY = ''

class Reviewer(object):
  MAX_CONTEXT = 5

  # Whitelisted patchwork hosts
  PATCHWORK_WHITELIST = [
    'lore.kernel.org',
    'patchwork.freedesktop.org',
    'patchwork.kernel.org',
    'patchwork.linuxtv.org',
    'patchwork.ozlabs.org'
  ]

  def __init__(self, verbose=False, chatty=False, git_dir=None):
    self.verbose = verbose
    self.chatty = chatty
    if git_dir:
      self.git_cmd = ['git', '-C', git_dir ]
    else:
      self.git_cmd = ['git']

  def __strip_commit_msg(self, patch):
    regex = re.compile('diff --git ')
    for i, l in enumerate(patch):
      if regex.match(l):
        return patch[i:]

  def __classify_line(self, line):
    for t in LineType:
      if re.match(t.value, line):
        return t
    return None

  def __strip_kruft(self, diff, context):
    ret = []
    ignore = [LineType.CHUNK, LineType.GITDIFF, LineType.INDEX,
              LineType.DELETED, LineType.ADDED, LineType.SIMILARITY,
              LineType.RENAME, LineType.EMPTY]
    include = [LineType.FILE_NEW, LineType.FILE_OLD, LineType.DIFF]
    ctx_counter = 0
    for l in diff:
      if not l:
        continue

      l_type = self.__classify_line(l)

      if self.chatty:
        print('%s- "%s"' % (l_type, l))

      if not l_type:
        sys.stderr.write('ERROR: Could not classify line "%s"\n' % l)
        ctx_counter = 0
        continue

      if l_type == LineType.CONTEXT:
        if ctx_counter < context:
          ret.append(l)
        ctx_counter += 1
        continue

      ctx_counter = 0

      if l_type in ignore:
        continue
      elif l_type in include:
        ret.append(l)
      else:
        sys.stderr.write('ERROR: line_type not handled {}: {}\n'.format(l_type,
                                                                        l))

    return ret

  def find_fixes_reference(self, sha, remote_name, branch):
    cmd = self.git_cmd + ['log', '--format=oneline', '--abbrev-commit', '-i',
                          '--grep', 'Fixes:.*{}'.format(sha[:8]),
                          '{}..{}/{}'.format(sha, remote_name, branch)]
    return subprocess.check_output(cmd).decode('UTF-8')

  def get_am_from_from_patch(self, patch):
    regex = re.compile('\(am from (http.*)\)', flags=re.I)
    m = regex.findall(patch)
    if not m or not len(m):
      return None
    return m

  def get_cherry_pick_shas_from_patch(self, patch):
    # This has pattern has gotten a bit hard to parse, so i'll do my best.

    # Start with an opening paren and allow for whitespace
    pattern = '\((?:\s*)'

    # This is pretty easy, look the "cherry picked from commit" string taking
    # into account space or dash between cherry and picked, and allowing for
    # multiple spaces after commit.
    pattern = 'cherry.picked from commit\s*'

    # Then we grab the hexidecimal hash string. Everything after this is
    # optional.
    pattern += '([0-9a-f]*)'

    # Optionally gobble up everything (including newlines with re.DOTALL) until
    # we hit the next group. This allows for extra fluff in between the hash and
    # URL (like an extra 'from' as seen in http://crosreview.com/1537900). The
    # first ? is because this is an optional group and the second one is to use
    # a non-greedy algorithm with the .*
    pattern += '(?:.*?)'

    # This will match the remote url. It's pretty simple, just matches any
    # protocol (git://, http://, https://, madeup://) and then match all
    # non-whitespace characters, this is our remote.
    pattern += '(\S*\://\S*)?'

    # Now that we have the URL, eat any whitespace again
    pattern += '(?:\s*)'

    # Now assume the next run of non-whitespace characters is the remote branch
    pattern += '(\S*)?'

    # Finally, account for any whitespace after the branch name and close the
    # paren
    pattern += '(?:\s*)\)'

    regex = re.compile(pattern, flags=(re.I | re.MULTILINE | re.DOTALL))
    m = regex.findall(patch)
    if not m or not len(m):
      return None
    ret = []
    for s in m:
      ret.append({'sha': s[0],
                  'remote': s[1] if s[1] else None,
                  'branch': s[2] if s[2] else None})
    return ret

  def generate_remote_name(self, remote):
    return re.sub('([a-z]*\://)|\W', '', remote, flags=re.I)

  def fetch_remote(self, remote_name, remote, branch):
    if self.verbose:
      print('Fetching {}/{} as {}'.format(remote, branch, remote_name))

    cmd = self.git_cmd + ['remote', 'add', remote_name, remote]
    # LAZY: Assuming if this fails the remote already exists
    subprocess.call(cmd, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)

    try:
      cmd = self.git_cmd + ['fetch', '--prune', remote_name, branch]
      subprocess.call(cmd, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
    except:
      cmd = self.git_cmd + ['remote', 'rm', remote_name]
      subprocess.call(cmd, stdout=subprocess.DEVNULL)
      raise

  def get_cherry_pick_sha_from_local_sha(self, local_sha):
    commit_message = (subprocess.check_output(['git', 'log', '-1', local_sha])
                        .decode('UTF-8'))
    # Use the last SHA found in the patch, since it's (probably) most recent,
    # and only return the SHA, not the remote/branch
    return self.get_cherry_pick_shas_from_patch(commit_message)[-1]['sha']

  def get_commit_from_patchwork(self, url):
    regex = re.compile('https://([a-z\.]*)/([a-z/]*)/([0-9]*)/')
    m = regex.match(url)
    if not m or not (m.group(1) in self.PATCHWORK_WHITELIST):
      sys.stderr.write('ERROR: URL "%s"\n' % url)
      return None
    return requests.get(url + 'raw/').text

  def is_sha_in_branch(self, sha, remote_name, branch):
    cmd = self.git_cmd + ['log', '--format=%H',
                          '{}^..{}/{}'.format(sha, remote_name, branch)]
    try:
      output = subprocess.check_output(cmd).decode('UTF-8')
    except:
      print('Exception encountered running {}'.format(cmd))
      return False
    return True if sha in output else False

  def get_commit_from_sha(self, sha):
    cmd = self.git_cmd + ['show', '--minimal', '-U{}'.format(self.MAX_CONTEXT),
                          r'--format=%B', sha]
    return subprocess.check_output(cmd).decode('UTF-8')

  def get_commit_from_remote(self, remote, ref):
    cmd = self.git_cmd + ['fetch', '--prune', remote, ref]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    return self.get_commit_from_sha('FETCH_HEAD')

  def compare_diffs(self, a, b, context=0):
    if context > self.MAX_CONTEXT:
      raise ValueError('Invalid context given')

    a = a.split('\n')
    b = b.split('\n')

    # strip the commit messages
    a = self.__strip_commit_msg(a) or []
    b = self.__strip_commit_msg(b) or []
    a = self.__strip_kruft(a, context)
    b = self.__strip_kruft(b, context)

    ret = []
    diff = difflib.unified_diff(a, b, n=0)
    for l in diff:
      ret.append(l)

    return ret

