use strict;
use warnings;

use Digest::SHA qw(sha256_hex);
use Fcntl qw(
  :DEFAULT
  FD_CLOEXEC
  F_SETFD
);
use JSON::PP ();

# CE-Q1's pre-Python verifier is evaluated from already-reviewed bytes by the
# literal /usr/bin/perl trampoline.  These host paths are intentionally code
# constants: the portable manifest names them only by the symbolic pathId.
my $INPUT_MANIFEST_RELATIVE =
  'docs/release-safety/ceq1-input-manifest.json';
my $TOOLCHAIN_MANIFEST_RELATIVE =
  'docs/release-safety/ceq1-toolchain-manifest.json';
my $HOST_PYTHON_ROOT =
  '/Users/baylorharrison/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none';
my $HOST_PYTHON = "$HOST_PYTHON_ROOT/bin/python3.12";
my $HOST_JDK_ROOT =
  '/opt/homebrew/Cellar/openjdk/25.0.2/libexec/openjdk.jdk/Contents/Home';
my $HOST_UV = '/Users/baylorharrison/.local/bin/uv';
my $HOST_FIRESTORE_JAR =
  '/Users/baylorharrison/.cache/firebase/emulators/cloud-firestore-emulator-v1.19.8.jar';
my $BOOTSTRAP_TARGET = 'scripts/bootstrap_ceq1_runtime.py';
my $RUN_TARGET = 'scripts/run_ceq1_env.py';
my $SEALED_PYTHON_RELATIVE = '.ceq1-venv/python/bin/python3.12';
my $WHEELHOUSE_RELATIVE = '.ceq1-runtime/wheelhouse';

my @INPUT_FILE_PATHS = (
  'docs/release-safety/ceq1-wheelhouse-manifest.json',
  'requirements-ceq1.in',
  'requirements-ceq1.lock',
  'requirements.lock',
  'scripts/bootstrap_ceq1_runtime.py',
  'scripts/build_ceq1_wheelhouse.py',
  'scripts/run_ceq1_env.py',
  'scripts/verify_ceq1_entry.pl',
);
my @POLICY_PLACEHOLDERS = (
  'BOOTSTRAP_SCRIPT',
  'BUILDER_SCRIPT',
  'BUNDLE',
  'FIRESTORE_JAR',
  'INPUT_MANIFEST',
  'JDK_ROOT',
  'PRODUCT_LOCK',
  'PYTHON_SOURCE',
  'QUALIFICATION_INPUT',
  'QUALIFICATION_LOCK',
  'READ_ANCESTOR_RULES',
  'RELOCATION',
  'REPO',
  'RUNTIME',
  'TOOLCHAIN_MANIFEST',
  'UV',
  'UV_CACHE',
  'VERIFIER_SCRIPT',
  'WHEELHOUSE_MANIFEST',
  'WRAPPER_SCRIPT',
);

my $OPENAT_SYSCALL = 463;    # macOS arm64 openat(2)
my $F_GETPATH = 50;          # macOS fcntl(2)
my $CLOSE_SYSCALL = 6;       # macOS close(2)
my $READ_CHUNK = 1024 * 1024;

sub _fail {
  my ($message) = @_;
  die "CE-Q1 entry blocked: $message\n";
}

sub _is_hash {
  my ($value) = @_;
  return defined($value) && !ref($value) && $value =~ /\A[0-9a-f]{64}\z/;
}

sub _is_uint {
  my ($value) = @_;
  return defined($value) && !ref($value) && $value =~ /\A(?:0|[1-9][0-9]*)\z/;
}

sub _keys_exact {
  my ($value, $expected, $label) = @_;
  _fail("$label is not an object") unless ref($value) eq 'HASH';
  my @actual = sort keys %{$value};
  my @wanted = sort @{$expected};
  _fail("$label keys drift") unless "@actual" eq "@wanted";
}

sub _array_exact {
  my ($value, $expected, $label) = @_;
  _fail("$label is not an array") unless ref($value) eq 'ARRAY';
  _fail("$label length drift") unless @{$value} == @{$expected};
  for my $index (0 .. $#{$expected}) {
    _fail("$label value drift")
      if ref($value->[$index]) || $value->[$index] ne $expected->[$index];
  }
}

sub _safe_relative_path {
  my ($path) = @_;
  return 0 if !defined($path) || ref($path) || $path eq '';
  return 0 if $path =~ /[^\x20-\x7e]/ || $path =~ m{\\};
  return 0 if $path =~ m{\A/} || $path =~ m{//} || $path =~ m{\A\./};
  my @parts = split m{/}, $path, -1;
  return 0 if grep { $_ eq '' || $_ eq '.' || $_ eq '..' } @parts;
  return 1;
}

sub _same_stat {
  my ($left, $right) = @_;
  # st_atime is deliberately excluded; a read is allowed to update it.
  for my $index (0, 1, 2, 3, 7, 9, 10) {
    return 0 if $left->[$index] != $right->[$index];
  }
  return 1;
}

sub _openat_handle {
  my ($directory, $name, $flags) = @_;
  _fail("unsafe path component: $name")
    if !defined($name) || $name eq '' || $name eq '.' || $name eq '..'
      || $name =~ m{/} || $name =~ /\0/;
  my ($number, $dirfd, $pathname, $open_flags, $mode) =
    ($OPENAT_SYSCALL, fileno($directory), $name, $flags, 0);
  my $fd = syscall($number, $dirfd, $pathname, $open_flags, $mode);
  _fail("cannot open path component $name: $!") if $fd < 0;
  open(my $handle, '<&=' . $fd)
    or do {
      my ($close_number, $close_fd) = ($CLOSE_SYSCALL, $fd);
      syscall($close_number, $close_fd);
      _fail("cannot bind path descriptor $name: $!");
    };
  return $handle;
}

sub _open_absolute {
  my ($path, $directory) = @_;
  _fail("path is not canonical absolute: $path")
    unless defined($path) && !ref($path) && $path =~ m{\A/}
      && $path !~ m{//} && $path !~ m{(?:\A|/)\.{1,2}(?:/|\z)};
  sysopen(my $current, '/', O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    or _fail("cannot open filesystem root: $!");
  my @parts = grep { length($_) } split m{/}, $path;
  _fail('refusing filesystem root as target') unless @parts;
  for my $index (0 .. $#parts) {
    my $want_directory = $index < $#parts || $directory;
    my $flags = O_RDONLY | O_NOFOLLOW;
    $flags |= O_DIRECTORY if $want_directory;
    my $next = _openat_handle($current, $parts[$index], $flags);
    close($current) or _fail("cannot close path descriptor: $!");
    $current = $next;
  }
  return $current;
}

sub _descriptor_path {
  my ($handle) = @_;
  my $buffer = "\0" x 4096;
  fcntl($handle, $F_GETPATH, $buffer)
    or _fail("cannot resolve repository descriptor: $!");
  $buffer =~ s/\0.*\z//s;
  _fail('repository descriptor path is not canonical absolute')
    unless $buffer =~ m{\A/} && $buffer !~ m{//};
  return $buffer;
}

sub _repo_root {
  sysopen(my $root, '.', O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    or _fail("cannot open repository root: $!");
  my $path = _descriptor_path($root);
  my $verified = _open_absolute($path, 1);
  my @left = stat($root);
  my @right = stat($verified);
  _fail('repository root identity drift') unless _same_stat(\@left, \@right);
  close($verified) or _fail("cannot close repository verifier: $!");
  close($root) or _fail("cannot close repository root: $!");
  return $path;
}

sub _absolute_repo_path {
  my ($root, $relative) = @_;
  _fail("unsafe repository path: $relative") unless _safe_relative_path($relative);
  return "$root/$relative";
}

sub _read_regular {
  my ($path, $maximum) = @_;
  my $handle = _open_absolute($path, 0);
  my @before = stat($handle);
  _fail("not a single-link regular file: $path")
    unless @before && -f _ && $before[3] == 1;
  _fail("file exceeds size limit: $path")
    if defined($maximum) && $before[7] > $maximum;
  my ($data, $remaining) = ('', $before[7]);
  while ($remaining) {
    my $requested = $remaining < $READ_CHUNK ? $remaining : $READ_CHUNK;
    my $read = sysread($handle, my $chunk, $requested);
    _fail("read failure for $path: $!") unless defined($read);
    _fail("short read for $path") unless $read > 0;
    $data .= $chunk;
    $remaining -= $read;
  }
  my $extra = sysread($handle, my $byte, 1);
  _fail("EOF check failed for $path: $!") unless defined($extra);
  _fail("file grew during read: $path") unless $extra == 0;
  my @after = stat($handle);
  _fail("file identity changed during read: $path")
    unless _same_stat(\@before, \@after);
  close($handle) or _fail("cannot close verified file $path: $!");
  return ($data, \@before);
}

sub _decode_json {
  my ($bytes, $label) = @_;
  my $value = eval { JSON::PP->new->utf8(1)->decode($bytes) };
  _fail("$label is not valid JSON") if $@ || ref($value) ne 'HASH';
  return $value;
}

sub _normalize_relative {
  my ($value) = @_;
  my @output;
  for my $part (split m{/}, $value, -1) {
    next if $part eq '' || $part eq '.';
    if ($part eq '..') {
      return undef unless @output;
      pop @output;
      next;
    }
    push @output, $part;
  }
  return join('/', @output);
}

sub _normalize_absolute {
  my ($value) = @_;
  return undef unless defined($value) && $value =~ m{\A/};
  my $relative = _normalize_relative(substr($value, 1));
  return undef unless defined($relative);
  return '/' . $relative;
}

sub _tree_entries {
  my ($root) = @_;
  my $root_handle = _open_absolute($root, 1);
  my @root_stat = stat($root_handle);
  _fail("tree root is not a directory: $root") unless -d _;
  my @entries;
  my $owner_uid = $<;
  my $owner_gid = 0 + $(;

  my $walk;
  $walk = sub {
    my ($absolute, $relative_parent, $held_directory) = @_;
    my @directory_before = stat($held_directory);
    opendir(my $listing, $absolute)
      or _fail("cannot enumerate tree directory $absolute: $!");
    my @listing_stat = stat($listing);
    _fail("tree directory pathname identity drift: $absolute")
      unless _same_stat(\@directory_before, \@listing_stat);
    my @names = sort grep { $_ ne '.' && $_ ne '..' } readdir($listing);
    closedir($listing) or _fail("cannot close tree listing $absolute: $!");

    for my $name (@names) {
      _fail("unsafe tree entry name below $absolute")
        if $name eq '' || $name =~ m{/} || $name =~ /\0/;
      my $relative = length($relative_parent)
        ? "$relative_parent/$name"
        : $name;
      my $path = "$absolute/$name";
      my @info = lstat($path);
      _fail("cannot inspect tree entry $path: $!") unless @info;
      my $mode = $info[2] & 07777;
      my $item = {
        relativePath => $relative,
        mode => $mode,
        uidClass => $info[4] == $owner_uid ? 'owner' : 'other',
        gidClass => $info[5] == $owner_gid ? 'owner' : 'other',
        symlinkTarget => undef,
        contentSha256 => undef,
      };

      if (-d _) {
        my $child = _open_absolute($path, 1);
        my @opened = stat($child);
        _fail("tree directory changed before read: $relative")
          unless _same_stat(\@info, \@opened);
        $item->{type} = 'directory';
        $walk->($path, $relative, $child);
        my @after = stat($child);
        _fail("tree directory changed during read: $relative")
          unless _same_stat(\@opened, \@after);
        close($child) or _fail("cannot close tree directory $relative: $!");
      }
      elsif (-f _) {
        _fail("hard link in verified tree: $relative") unless $info[3] == 1;
        my ($bytes, $opened) = _read_regular($path, undef);
        _fail("tree file changed before read: $relative")
          unless _same_stat(\@info, $opened);
        $item->{type} = 'file';
        $item->{contentSha256} = sha256_hex($bytes);
      }
      elsif (-l _) {
        my $target = readlink($path);
        _fail("cannot read tree symlink $relative: $!") unless defined($target);
        my @after = lstat($path);
        _fail("tree symlink changed during read: $relative")
          unless _same_stat(\@info, \@after);
        if ($target =~ m{\A/}) {
          my $normalized = _normalize_absolute($target);
          _fail("invalid absolute tree symlink: $relative") unless defined($normalized);
          _fail("escaping tree symlink: $relative")
            unless $normalized eq $root || index($normalized, "$root/") == 0;
        }
        else {
          my $parent = $relative;
          $parent =~ s{/[^/]+\z}{};
          $parent = '' if $parent eq $relative;
          my $combined = length($parent) ? "$parent/$target" : $target;
          my $normalized = _normalize_relative($combined);
          _fail("escaping tree symlink: $relative") unless defined($normalized);
        }
        $item->{type} = 'symlink';
        $item->{symlinkTarget} = $target;
      }
      else {
        _fail("special entry in verified tree: $relative");
      }
      push @entries, $item;
    }
    my @directory_after = stat($held_directory);
    _fail("tree directory changed during scan: $relative_parent")
      unless _same_stat(\@directory_before, \@directory_after);
  };

  $walk->($root, '', $root_handle);
  my @root_after = stat($root_handle);
  _fail("tree root changed during scan: $root")
    unless _same_stat(\@root_stat, \@root_after);
  close($root_handle) or _fail("cannot close tree root $root: $!");
  return [sort { $a->{relativePath} cmp $b->{relativePath} } @entries];
}

sub _tree_receipt {
  my ($root) = @_;
  my $entries = _tree_entries($root);
  my $canonical = JSON::PP->new->canonical(1)->ascii(1)->encode($entries);
  return {
    algorithmVersion => 'ceq1-tree-v1',
    entryCount => scalar(@{$entries}),
    treeDigest => sha256_hex($canonical),
  };
}

sub _require_tree_receipt {
  my ($record, $actual, $label, $with_launcher) = @_;
  my @keys = qw(algorithmVersion entryCount treeDigest);
  push @keys, 'launcherSha256' if $with_launcher;
  _keys_exact($record, \@keys, $label);
  _fail("$label algorithm drift")
    unless $record->{algorithmVersion} eq 'ceq1-tree-v1';
  _fail("$label entry count is invalid") unless _is_uint($record->{entryCount});
  _fail("$label tree digest is invalid") unless _is_hash($record->{treeDigest});
  for my $key (qw(algorithmVersion entryCount treeDigest)) {
    _fail("$label does not match verified tree")
      unless $record->{$key} eq $actual->{$key};
  }
  if ($with_launcher) {
    _fail("$label launcher hash is invalid") unless _is_hash($record->{launcherSha256});
    my ($launcher) = _read_regular($HOST_PYTHON, undef);
    _fail("$label launcher drift")
      unless sha256_hex($launcher) eq $record->{launcherSha256};
  }
}

sub _verify_input_schema {
  my ($root, $manifest) = @_;
  _keys_exact(
    $manifest,
    [qw(schemaVersion algorithmVersion files trees portablePolicy platformTrust)],
    'input manifest',
  );
  _fail('input manifest schema version drift') unless $manifest->{schemaVersion} == 1;
  _fail('input manifest algorithm drift')
    unless $manifest->{algorithmVersion} eq 'ceq1-input-v1';

  _keys_exact($manifest->{files}, \@INPUT_FILE_PATHS, 'input files');
  my %verified_files;
  for my $relative (@INPUT_FILE_PATHS) {
    _fail("unsafe input file path: $relative") unless _safe_relative_path($relative);
    my $record = $manifest->{files}{$relative};
    _keys_exact($record, [qw(sha256 size)], "input file $relative");
    _fail("input file hash is invalid: $relative") unless _is_hash($record->{sha256});
    _fail("input file size is invalid: $relative") unless _is_uint($record->{size});
    my ($bytes, $info) = _read_regular(_absolute_repo_path($root, $relative), undef);
    _fail("input file size drift: $relative") unless length($bytes) == $record->{size};
    _fail("input file hash drift: $relative")
      unless sha256_hex($bytes) eq $record->{sha256};
    $verified_files{$relative} = {
      bytes => $bytes,
      sha256 => $record->{sha256},
      size => $record->{size},
    };
  }

  _keys_exact($manifest->{trees}, [qw(cpythonSource openjdkSource)], 'input trees');
  my $python_tree = _tree_receipt($HOST_PYTHON_ROOT);
  _require_tree_receipt(
    $manifest->{trees}{cpythonSource}, $python_tree, 'CPython source', 1,
  );
  my $jdk_tree = _tree_receipt($HOST_JDK_ROOT);
  _require_tree_receipt(
    $manifest->{trees}{openjdkSource}, $jdk_tree, 'OpenJDK source', 0,
  );

  _keys_exact(
    $manifest->{portablePolicy},
    [qw(templateSha256 placeholders)],
    'portable policy',
  );
  _fail('portable policy hash is invalid')
    unless _is_hash($manifest->{portablePolicy}{templateSha256});
  _array_exact(
    $manifest->{portablePolicy}{placeholders},
    \@POLICY_PLACEHOLDERS,
    'portable policy placeholders',
  );
  my $bootstrap = $verified_files{$BOOTSTRAP_TARGET}{bytes};
  my ($template) =
    $bootstrap =~ /BOOTSTRAP_SEATBELT_TEMPLATE = r'''(.*?)'''\r?\n\r?\n/s;
  _fail('cannot locate portable Seatbelt template in verified bootstrap')
    unless defined($template);
  _fail('portable Seatbelt template hash drift')
    unless sha256_hex($template) eq $manifest->{portablePolicy}{templateSha256};

  _keys_exact(
    $manifest->{platformTrust},
    [qw(perl uv firestoreJar)],
    'platform trust',
  );
  my $perl = $manifest->{platformTrust}{perl};
  _keys_exact($perl, [qw(pathId ownerUid requiredModules)], 'Perl trust');
  _fail('Perl path identifier drift') unless $perl->{pathId} eq 'APPLE_SYSTEM_PERL';
  _fail('Perl owner requirement drift') unless $perl->{ownerUid} == 0;
  _array_exact(
    $perl->{requiredModules},
    [qw(Digest::SHA Fcntl JSON::PP)],
    'Perl required modules',
  );
  _fail('entry is not running under Apple system Perl') unless $^X eq '/usr/bin/perl';
  my @perl_info = lstat('/usr/bin/perl');
  _fail('Apple system Perl trust prerequisite failed')
    unless @perl_info && -f _ && $perl_info[4] == 0;

  for my $specification (
    ['uv', 'PINNED_UV', $HOST_UV],
    ['firestoreJar', 'FIRESTORE_JAR_1_19_8', $HOST_FIRESTORE_JAR],
  ) {
    my ($name, $path_id, $path) = @{$specification};
    my $record = $manifest->{platformTrust}{$name};
    _keys_exact($record, [qw(pathId sha256 size)], "$name trust");
    _fail("$name path identifier drift") unless $record->{pathId} eq $path_id;
    _fail("$name hash is invalid") unless _is_hash($record->{sha256});
    _fail("$name size is invalid") unless _is_uint($record->{size});
    my ($bytes) = _read_regular($path, undef);
    _fail("$name size drift") unless length($bytes) == $record->{size};
    _fail("$name hash drift") unless sha256_hex($bytes) eq $record->{sha256};
  }
  return (\%verified_files, $python_tree, $jdk_tree);
}

sub verify_inputs {
  my ($root, $reviewed_hash) = @_;
  _fail('reviewed input-manifest hash is invalid') unless _is_hash($reviewed_hash);
  my ($bytes) = _read_regular(
    _absolute_repo_path($root, $INPUT_MANIFEST_RELATIVE),
    1024 * 1024,
  );
  _fail('input-manifest reviewed hash mismatch')
    unless sha256_hex($bytes) eq $reviewed_hash;
  my $manifest = _decode_json($bytes, 'input manifest');
  my ($files, $python_tree, $jdk_tree) = _verify_input_schema($root, $manifest);
  return ($manifest, $files, $python_tree, $jdk_tree);
}

sub _require_record_keys_and_hashes {
  my ($record, $keys, $label) = @_;
  _keys_exact($record, $keys, $label);
  for my $key (grep { /Sha256\z/ || $_ eq 'sha256' || $_ eq 'treeDigest' } @{$keys}) {
    _fail("$label $key is invalid") unless _is_hash($record->{$key});
  }
}

sub _verify_wheelhouse {
  my ($root, $wheel_manifest_bytes) = @_;
  my $manifest = _decode_json($wheel_manifest_bytes, 'wheelhouse manifest');
  _keys_exact(
    $manifest,
    [qw(schemaVersion algorithmVersion python builderSha256 packages)],
    'wheelhouse manifest',
  );
  _fail('wheelhouse manifest schema drift') unless $manifest->{schemaVersion} == 1;
  _fail('wheelhouse manifest algorithm drift')
    unless $manifest->{algorithmVersion} eq 'ceq1-derived-wheel-v1';
  _fail('wheelhouse package list is invalid') unless ref($manifest->{packages}) eq 'ARRAY';
  my %expected;
  for my $package (@{$manifest->{packages}}) {
    _fail('wheelhouse package is not an object') unless ref($package) eq 'HASH';
    my $wheel = $package->{wheel};
    _fail('wheelhouse wheel is not an object') unless ref($wheel) eq 'HASH';
    my $filename = $wheel->{filename};
    _fail('unsafe wheel filename')
      unless defined($filename) && !ref($filename)
        && $filename =~ /\A[A-Za-z0-9_.+-]+\.whl\z/ && $filename !~ /\.\./;
    _fail("duplicate wheel filename: $filename") if exists($expected{$filename});
    _fail("invalid wheel hash: $filename") unless _is_hash($wheel->{sha256});
    _fail("invalid wheel size: $filename") unless _is_uint($wheel->{size});
    $expected{$filename} = { sha256 => $wheel->{sha256}, size => $wheel->{size} };
  }

  my $directory = _absolute_repo_path($root, $WHEELHOUSE_RELATIVE);
  my $held = _open_absolute($directory, 1);
  my @directory_info = stat($held);
  _fail('wheelhouse root mode drift') unless ($directory_info[2] & 07777) == 0555;
  opendir(my $listing, $directory) or _fail("cannot enumerate wheelhouse: $!");
  my @listing_info = stat($listing);
  _fail('wheelhouse root identity drift')
    unless _same_stat(\@directory_info, \@listing_info);
  my @actual = sort grep { $_ ne '.' && $_ ne '..' } readdir($listing);
  closedir($listing) or _fail("cannot close wheelhouse listing: $!");
  my @wanted = sort keys %expected;
  _fail('wheelhouse member closure drift') unless "@actual" eq "@wanted";
  for my $filename (@wanted) {
    my ($bytes, $info) = _read_regular("$directory/$filename", undef);
    _fail("wheel mode drift: $filename") unless ($info->[2] & 07777) == 0444;
    _fail("wheel size drift: $filename") unless length($bytes) == $expected{$filename}{size};
    _fail("wheel hash drift: $filename")
      unless sha256_hex($bytes) eq $expected{$filename}{sha256};
  }
  my @directory_after = stat($held);
  _fail('wheelhouse changed during verification')
    unless _same_stat(\@directory_info, \@directory_after);
  close($held) or _fail("cannot close wheelhouse: $!");
}

sub _verify_run_outputs {
  my (
    $root, $input_hash, $input, $files, $python_tree, $jdk_tree,
    $reviewed_toolchain_hash,
  ) = @_;
  _fail('reviewed toolchain-manifest hash is invalid')
    unless _is_hash($reviewed_toolchain_hash);
  my ($bytes) = _read_regular(
    _absolute_repo_path($root, $TOOLCHAIN_MANIFEST_RELATIVE),
    1024 * 1024,
  );
  _fail('toolchain-manifest reviewed hash mismatch')
    unless sha256_hex($bytes) eq $reviewed_toolchain_hash;
  my $toolchain = _decode_json($bytes, 'toolchain manifest');
  _keys_exact(
    $toolchain,
    [qw(schemaVersion algorithmVersion artifacts lockfiles wheelhouseManifestSha256 inputManifestSha256 bootstrapSha256 builderSha256 seatbeltTemplate sealedRuntime)],
    'toolchain manifest',
  );
  _fail('toolchain schema version drift') unless $toolchain->{schemaVersion} == 1;
  _fail('toolchain algorithm drift')
    unless $toolchain->{algorithmVersion} eq 'ceq1-toolchain-v1';
  _fail('toolchain input-manifest binding drift')
    unless $toolchain->{inputManifestSha256} eq $input_hash;

  my $wheel_relative = 'docs/release-safety/ceq1-wheelhouse-manifest.json';
  _fail('toolchain wheelhouse-manifest binding drift')
    unless $toolchain->{wheelhouseManifestSha256} eq $files->{$wheel_relative}{sha256};
  _fail('toolchain bootstrap binding drift')
    unless $toolchain->{bootstrapSha256} eq $files->{$BOOTSTRAP_TARGET}{sha256};
  _fail('toolchain builder binding drift')
    unless $toolchain->{builderSha256}
      eq $files->{'scripts/build_ceq1_wheelhouse.py'}{sha256};

  _keys_exact($toolchain->{lockfiles}, [qw(product qualification)], 'toolchain locks');
  _fail('toolchain product lock drift')
    unless $toolchain->{lockfiles}{product} eq $files->{'requirements.lock'}{sha256};
  _fail('toolchain qualification lock drift')
    unless $toolchain->{lockfiles}{qualification}
      eq $files->{'requirements-ceq1.lock'}{sha256};

  _keys_exact(
    $toolchain->{seatbeltTemplate},
    [qw(sha256 placeholders)],
    'toolchain Seatbelt template',
  );
  _fail('toolchain Seatbelt template hash drift')
    unless $toolchain->{seatbeltTemplate}{sha256}
      eq $input->{portablePolicy}{templateSha256};
  _array_exact(
    $toolchain->{seatbeltTemplate}{placeholders},
    \@POLICY_PLACEHOLDERS,
    'toolchain Seatbelt placeholders',
  );

  my $artifacts = $toolchain->{artifacts};
  _keys_exact(
    $artifacts,
    [qw(cpythonSource openjdkSource uv firestoreJar zipfile entryVerifier directWrapper)],
    'toolchain artifacts',
  );
  _require_record_keys_and_hashes(
    $artifacts->{cpythonSource},
    [qw(algorithmVersion entryCount treeDigest launcherSha256 version versionOutputSha256)],
    'toolchain CPython',
  );
  for my $key (qw(algorithmVersion entryCount treeDigest launcherSha256)) {
    _fail('toolchain CPython input binding drift')
      unless $artifacts->{cpythonSource}{$key} eq $input->{trees}{cpythonSource}{$key};
  }
  _fail('toolchain CPython version drift')
    unless $artifacts->{cpythonSource}{version} eq '3.12.13';
  _require_record_keys_and_hashes(
    $artifacts->{openjdkSource},
    [qw(algorithmVersion entryCount treeDigest version versionOutputSha256)],
    'toolchain OpenJDK',
  );
  for my $key (qw(algorithmVersion entryCount treeDigest)) {
    _fail('toolchain OpenJDK input binding drift')
      unless $artifacts->{openjdkSource}{$key} eq $input->{trees}{openjdkSource}{$key};
  }
  _fail('toolchain OpenJDK version drift')
    unless $artifacts->{openjdkSource}{version} eq '25.0.2';
  _require_record_keys_and_hashes(
    $artifacts->{uv}, [qw(sha256 version versionOutputSha256)], 'toolchain uv',
  );
  _fail('toolchain uv input binding drift')
    unless $artifacts->{uv}{sha256} eq $input->{platformTrust}{uv}{sha256};
  _fail('toolchain uv version drift') unless $artifacts->{uv}{version} eq '0.11.3';
  _require_record_keys_and_hashes(
    $artifacts->{firestoreJar},
    [qw(sha256 size version versionOutputSha256)],
    'toolchain Firestore JAR',
  );
  _fail('toolchain Firestore JAR input binding drift')
    unless $artifacts->{firestoreJar}{sha256}
      eq $input->{platformTrust}{firestoreJar}{sha256}
      && $artifacts->{firestoreJar}{size}
        == $input->{platformTrust}{firestoreJar}{size};
  _fail('toolchain Firestore JAR version drift')
    unless $artifacts->{firestoreJar}{version} eq '1.19.8';
  _require_record_keys_and_hashes($artifacts->{zipfile}, [qw(sha256)], 'toolchain zipfile');
  my $wheel_manifest = _decode_json($files->{$wheel_relative}{bytes}, 'wheelhouse manifest');
  _fail('toolchain zipfile binding drift')
    unless $artifacts->{zipfile}{sha256} eq $wheel_manifest->{python}{zipfileSha256};
  _require_record_keys_and_hashes(
    $artifacts->{entryVerifier}, [qw(sha256)], 'toolchain entry verifier',
  );
  _fail('toolchain entry-verifier binding drift')
    unless $artifacts->{entryVerifier}{sha256}
      eq $files->{'scripts/verify_ceq1_entry.pl'}{sha256};
  _require_record_keys_and_hashes(
    $artifacts->{directWrapper}, [qw(sha256)], 'toolchain direct wrapper',
  );
  _fail('toolchain direct-wrapper binding drift')
    unless $artifacts->{directWrapper}{sha256} eq $files->{$RUN_TARGET}{sha256};

  _keys_exact(
    $toolchain->{sealedRuntime},
    [qw(algorithmVersion entryCount treeDigest)],
    'sealed runtime',
  );
  my $runtime = _tree_receipt(_absolute_repo_path($root, '.ceq1-venv'));
  for my $key (qw(algorithmVersion entryCount treeDigest)) {
    _fail('sealed runtime output drift')
      unless $toolchain->{sealedRuntime}{$key} eq $runtime->{$key};
  }
  my ($sealed_launcher) = _read_regular(
    _absolute_repo_path($root, $SEALED_PYTHON_RELATIVE), undef,
  );
  _fail('sealed Python launcher drift')
    unless sha256_hex($sealed_launcher) eq $input->{trees}{cpythonSource}{launcherSha256};
  _verify_wheelhouse($root, $files->{$wheel_relative}{bytes});
}

sub _close_unrelated_descriptors {
  my ($keep) = @_;
  opendir(my $directory, '/dev/fd')
    or _fail("cannot enumerate child descriptors: $!");
  my @descriptors = grep { /\A[0-9]+\z/ } readdir($directory);
  closedir($directory) or _fail("cannot close descriptor listing: $!");
  for my $descriptor (@descriptors) {
    next if $descriptor <= 2 || $descriptor == $keep;
    my ($number, $fd) = ($CLOSE_SYSCALL, 0 + $descriptor);
    syscall($number, $fd);
  }
}

sub run_verified_python {
  my ($python, $target_path, $target_bytes, $target_arguments) = @_;
  pipe(my $reader, my $writer) or _fail("cannot create verified target pipe: $!");
  fcntl($reader, F_SETFD, 0)
    or _fail("cannot make verified target pipe inheritable: $!");
  my $reader_fd = fileno($reader);
  my $length = length($target_bytes);
  my $loader = <<'PYTHON_LOADER';
import os, sys
fd = int(sys.argv[1])
size = int(sys.argv[2])
path = sys.argv[3]
target_argv = [path, *sys.argv[4:]]
chunks = []
remaining = size
while remaining:
    chunk = os.read(fd, min(1048576, remaining))
    if not chunk:
        raise RuntimeError("short verified target read")
    chunks.append(chunk)
    remaining -= len(chunk)
if os.read(fd, 1) != b"":
    raise RuntimeError("verified target grew during pipe read")
os.close(fd)
source = b"".join(chunks)
code = compile(source, "<ceq1-verified-target>", "exec", dont_inherit=True)
sys.argv = target_argv
scope = {
    "__name__": "__main__",
    "__file__": path,
    "__package__": None,
    "__spec__": None,
    "__builtins__": __builtins__,
}
exec(code, scope, scope)
PYTHON_LOADER

  my $pid = fork();
  _fail("cannot fork verified target: $!") unless defined($pid);
  if ($pid == 0) {
    close($writer);
    _close_unrelated_descriptors($reader_fd);
    %ENV = (PATH => '/usr/bin:/bin', LANG => 'C', LC_ALL => 'C');
    exec {$python} (
      $python, '-I', '-S', '-B', '-c', $loader,
      "$reader_fd", "$length", $target_path, @{$target_arguments},
    ) or do {
      print STDERR "CE-Q1 entry blocked: cannot exec verified Python: $!\n";
      exit 126;
    };
  }

  close($reader) or _fail("cannot close parent pipe reader: $!");
  local $SIG{PIPE} = 'IGNORE';
  my ($offset, $write_error) = (0, undef);
  while ($offset < $length) {
    my $written = syswrite($writer, $target_bytes, $length - $offset, $offset);
    if (!defined($written) || $written <= 0) {
      $write_error = "$!";
      last;
    }
    $offset += $written;
  }
  $write_error = 'short verified target write' if !$write_error && $offset != $length;
  close($writer) or $write_error ||= "$!";
  my $waited = waitpid($pid, 0);
  _fail('waitpid did not reap the verified target') unless $waited == $pid;
  my $status = $?;
  _fail("verified target pipe write failed: $write_error") if $write_error;
  if ($status & 127) {
    my $signal = $status & 127;
    kill($signal, $$);
    _fail("cannot propagate verified target signal $signal");
  }
  exit($status >> 8);
}

sub _main {
  my @arguments = @_;
  my $mode = shift @arguments;
  _fail('mode must be bootstrap or run')
    unless defined($mode) && ($mode eq 'bootstrap' || $mode eq 'run');
  my $input_hash = shift @arguments;
  my $toolchain_hash;
  $toolchain_hash = shift @arguments if $mode eq 'run';
  _fail('missing verified-entry argument delimiter')
    unless @arguments && shift(@arguments) eq '--';
  my $root = _repo_root();
  my ($input, $files, $python_tree, $jdk_tree) = verify_inputs($root, $input_hash);

  my ($python, $target_relative, @target_arguments);
  if ($mode eq 'bootstrap') {
    _fail('bootstrap target arguments are not closed')
      unless @arguments == 1
        && ($arguments[0] eq 'prepare' || $arguments[0] eq 'derive-review-candidate');
    $python = $HOST_PYTHON;
    $target_relative = $BOOTSTRAP_TARGET;
    @target_arguments = @arguments;
  }
  else {
    my @prefix = (
      './.ceq1-venv/python/bin/python3.12',
      '-I', '-S', '-B',
      'scripts/run_ceq1_env.py',
    );
    _fail('run target vector is incomplete') unless @arguments >= @prefix;
    for my $index (0 .. $#prefix) {
      _fail('run target vector drift') unless $arguments[$index] eq $prefix[$index];
    }
    splice(@arguments, 0, scalar(@prefix));
    _verify_run_outputs(
      $root, $input_hash, $input, $files, $python_tree, $jdk_tree,
      $toolchain_hash,
    );
    $python = _absolute_repo_path($root, $SEALED_PYTHON_RELATIVE);
    $target_relative = $RUN_TARGET;
    @target_arguments = @arguments;
  }

  my $target_path = _absolute_repo_path($root, $target_relative);
  my $target_bytes = $files->{$target_relative}{bytes};
  run_verified_python($python, $target_path, $target_bytes, \@target_arguments);
  _fail('verified Python unexpectedly returned');
}

_main(@ARGV);
