"""Derive native Swift test commands from project metadata, never agent shell text."""
from pathlib import Path
import re
import shlex
import shutil
import xml.etree.ElementTree as ET


IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def command(root, name, prefix):
    root, relative = Path(root).resolve(), Path(name)
    if relative.suffix != '.swift' or not any('test' in part.lower() for part in relative.parts):
        return None
    path = root / relative
    for parent in path.parents:
        if not parent.is_relative_to(root):
            break
        manifest = parent / 'Package.swift'
        if manifest.is_file() and manifest.resolve() == manifest:
            return shlex.join([*prefix, '--runner', 'swift', '--executable', shutil.which('swift') or 'swift',
                               '--manifest', str(manifest.relative_to(root))])
    matches = []
    for parent in path.parents:
        if not parent.is_relative_to(root):
            break
        for scheme in sorted(parent.glob('*.xcodeproj/xcshareddata/xcschemes/*.xcscheme'))[:20]:
            project = scheme.parents[2]
            if scheme.resolve() != scheme or scheme.stat().st_size > 512 * 1024:
                continue
            try:
                document = ET.parse(scheme).getroot()
                for test in document.findall('./TestAction/Testables/TestableReference'):
                    if test.get('skipped') == 'YES':
                        continue
                    reference = test.find('BuildableReference')
                    if reference is None:
                        continue
                    target = reference.get('BlueprintName', '')
                    if (IDENTIFIER.fullmatch(target) and target in relative.parts
                            and reference.get('BuildableName') == target + '.xctest'
                            and reference.get('ReferencedContainer') == 'container:' + project.name):
                        matches.append((project, scheme.stem, target))
            except (OSError, ET.ParseError):
                continue
    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        return None
    project, scheme, target = matches[0]
    return shlex.join([*prefix, '--runner', 'xcode', '--executable', shutil.which('xcodebuild') or 'xcodebuild',
                       '--manifest', str(project.relative_to(root)), '--scheme', scheme, '--target', target])
