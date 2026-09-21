project('pam_faceid', 'c',
  version: '0.1.0',
  default_options: ['c_std=c11', 'warning_level=3', 'werror=true'])

pam_dep = meson.get_compiler('c').find_library('pam')

shared_library('pam_faceid',
  'pam_faceid.c',
  name_prefix: '',
  dependencies: [pam_dep],
  install: true,
  install_dir: get_option('libdir') / 'security')
