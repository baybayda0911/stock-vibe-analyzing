"""Read, compare, package and back up private research through an explicit Lab tool.

Python standard library only. No network, account connection or global install.
The configured research_lab.py supplies the existing record protocol and guards.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import unquote, urlsplit


def load_backend(value):
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or path.name!='research_lab.py' or not path.is_file():
        raise ValueError('Specify an existing absolute research_lab.py tool path')
    for part in (path,*path.parents):
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Linked/reparse tool paths are forbidden')
    spec=importlib.util.spec_from_file_location('archive_lab_backend',path)
    lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
    if getattr(lab,'SCHEMA',None)!='stock-research-lab/1':
        raise ValueError('Unsupported Lab tool protocol')
    for name in ('lab_root','checked_path','inside','context','scan','writer','reindex','copy_source'):
        if not callable(getattr(lab,name,None)):
            raise ValueError('Lab tool lacks required operation: '+name)
    return lab


def sibling(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def output_path(lab,value,root):
    target=lab.checked_path(value,reject_git=True)
    if target==target.parent or os.path.lexists(target):
        raise FileExistsError('Output must be a new private directory, not a drive root')
    if any((parent/'SKILL.md').is_file() for parent in target.parents):
        raise ValueError('Do not write private output inside an installed Skill')
    if target.is_relative_to(root):
        workspace=root/'_workspace'/'views'
        if target==workspace or not target.is_relative_to(workspace):
            raise ValueError('Lab-internal views/packages belong below _workspace/views')
    for parent in target.parents:
        if (parent/'config.local.json').is_file() and target.is_relative_to(parent/'securities'):
            raise ValueError('Output cannot be inside authoritative security records')
    return target


def publish(lab,target,operation):
    target.parent.mkdir(parents=True,exist_ok=True)
    lab.checked_path(target,reject_git=True)
    stage=Path(tempfile.mkdtemp(prefix='.archive-view-',dir=target.parent))
    try:
        result=operation(stage)
        if os.path.lexists(target):
            raise FileExistsError('Refusing to overwrite an existing output')
        stage.rename(target)
        return result
    except BaseException:
        if stage.is_dir() and not stage.is_symlink() and stage.parent==target.parent:
            lab.checked_path(stage,reject_git=True)
            shutil.rmtree(stage)
        raise


def save_json(path,value):
    with path.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')


def resolved_roles(lab,root,context):
    """Use the shared classifier; an older backend never guesses content roles."""
    version='stock-research-role-resolver/1'
    if (getattr(lab,'CAPABILITIES',{}).get('role_resolution')!=version or
            not callable(getattr(lab,'resolve_roles',None))):
        return None
    roles,_,_=lab.resolve_roles(root,context)
    if (not isinstance(roles,dict) or roles.get('schema_version')!='stock-research-roles/1' or
            roles.get('resolver_version')!=version):
        raise ValueError('Unsupported shared content-role resolver')
    return roles


def registered_original(lab,root,context,result,manifest):
    """Resolve one explicit registered original, never a report link or user path."""
    entries=[entry for entry in manifest.get('attachments',[])
             if entry.get('relative_path')=='research-components.json']
    if not entries:
        return None
    directory=lab.inside(root,*Path(result['path']).parent.parts)
    component=lab.read_json(directory/'research-components.json')
    if not isinstance(component,dict):
        raise ValueError('Research components must be one object')
    reference=component.get('source_ref')
    if reference is None:
        roles=resolved_roles(lab,root,context)
        if roles and any(row['result_id']==result['result_id'] and row['content_role'] in
                         {'broker_source','source_adaptation'} for row in roles['classified']):
            return dict(gap='missing_registered_source_ref')
        return None
    if not isinstance(reference,dict) or set(reference)!={'security_id','import_id','original_sha256'}:
        raise ValueError('Source reference must identify one registered original without a path')
    if reference['security_id']!=context['security_id']:
        raise ValueError('Source reference belongs to another security')
    lab.identifier(reference['import_id'],'source_ref.import_id')
    if not isinstance(reference['original_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',reference['original_sha256']):
        raise ValueError('Source reference requires its original SHA256')
    roles=resolved_roles(lab,root,context)
    if roles is None:
        raise ValueError('Source-linked export requires the shared content-role resolver')
    classified=[row for row in roles['classified'] if row['result_id']==result['result_id']]
    if len(classified)!=1 or classified[0]['content_role'] not in {'broker_source','source_adaptation'}:
        raise ValueError('Registered original requires a verified supplementary source role')
    imports=[row for row in lab.scan(root)['imports'] if row['import_id']==reference['import_id']]
    if len(imports)!=1:
        raise ValueError('Referenced original import is missing')
    registration=imports[0]
    if (registration.get('schema_version')!=lab.SCHEMA or
            registration.get('security_id')!=context['security_id'] or
            registration.get('result_id')!=result['result_id'] or
            result.get('import_id')!=reference['import_id'] or
            registration.get('original_sha256')!=reference['original_sha256'] or
            manifest.get('original_sha256')!=reference['original_sha256']):
        raise ValueError('Registered original identity or fingerprint differs from its adaptation')
    if any(registration.get(key)!=manifest.get(key) for key in
           ('module','research_asof','data_asof','original_generated_at','original_date_evidence')):
        raise ValueError('Registered original dates or provenance differ from its frozen adaptation')
    cutoff=lab.stamp(context['asof'],'asof')
    imported=lab.stamp(registration.get('imported_at'),'imported_at')
    if imported>cutoff or imported>lab.stamp(result['generated_at'],'generated_at'):
        raise ValueError('Registered original import is not available at this cutoff')
    if registration.get('original_generated_at') is not None:
        original_at=lab.stamp(registration['original_generated_at'],'original_generated_at')
        if original_at>imported or not registration.get('original_date_evidence'):
            raise ValueError('Original publication date exceeds import or lacks evidence')
    expected=PurePosixPath('securities',context['security_id'],'imports',reference['import_id'],'import.json').as_posix()
    if registration.get('path')!=expected:
        raise ValueError('Registered original metadata is outside its assigned import directory')
    name=registration.get('source_name')
    if (not isinstance(name,str) or not name or '/' in name or '\\' in name or ':' in name or
            name in {'.','..'} or Path(name).name!=name):
        raise ValueError('Registered original filename must be one safe path component')
    original=lab.inside(root,*PurePosixPath(expected).parent.parts,'original',name)
    source=lab.checked_path(original,reject_git=True)
    if not source.is_file() or source.stat().st_nlink!=1:
        raise ValueError('Registered original must be one ordinary file')
    if hashlib.sha256(source.read_bytes()).hexdigest()!=reference['original_sha256']:
        raise ValueError('Registered original fingerprint mismatch')
    return dict(reference=reference,registration=registration,source=source,
                metadata=lab.inside(root,*PurePosixPath(expected).parts),role=classified[0])


def package_result(lab,root,security_id,asof,result_id,output):
    root=lab.lab_root(root)
    context=lab.context(root,security_id,asof)
    matches=[row for row in context['results'] if row['result_id']==result_id]
    if len(matches)!=1:
        raise ValueError('Result is missing or not available for this security/asof')
    result=matches[0]
    directory=lab.inside(root,*Path(result['path']).parent.parts)
    manifest=lab.read_json(directory/'manifest.json')
    original=registered_original(lab,root,context,result,manifest)
    target=output_path(lab,output,root)
    entries=[dict(relative_path=name) for name in ('report.md','manifest.json','items.json')]+manifest.get('attachments',[])
    def copy(stage):
        files=[]
        for entry in entries:
            relative=entry['relative_path']
            source=lab.inside(directory,*Path(relative).parts)
            destination=stage/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            digest=lab.copy_source(source,destination)
            if entry.get('sha256') and digest!=entry['sha256']:
                raise ValueError('Attachment changed during export')
            files.append(dict(relative_path=relative,sha256=digest,size_bytes=destination.stat().st_size))
        for name,key in (('report.md','report_sha256'),('items.json','items_sha256')):
            copied=next(row for row in files if row['relative_path']==name)
            if copied['sha256']!=manifest[key]:
                raise ValueError('Frozen record changed during export')
        originals=[]
        gaps=[original['gap']] if original is not None and original.get('gap') else []
        if original is not None and not gaps:
            registration=original['registration']
            prefix=PurePosixPath('original-sources',registration['import_id'])
            copied={}
            for source,name in ((original['source'],registration['source_name']),
                                (original['metadata'],'import.json')):
                relative=(prefix/'original'/name if source==original['source'] else prefix/name).as_posix()
                destination=stage.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True,exist_ok=True)
                digest=lab.copy_source(source,destination)
                if source==original['source'] and digest!=original['reference']['original_sha256']:
                    raise ValueError('Registered original changed during export')
                if source==original['metadata'] and lab.read_json(destination)!={
                        key:value for key,value in registration.items() if key!='path'}:
                    raise ValueError('Registered original metadata changed during export')
                copied['original' if source==original['source'] else 'metadata']=relative
                files.append(dict(relative_path=relative,sha256=digest,size_bytes=destination.stat().st_size))
            role=original['role']
            originals.append(dict(source_ref=original['reference'],original_path=copied['original'],
                import_metadata_path=copied['metadata'],imported_at=registration['imported_at'],
                original_generated_at=registration.get('original_generated_at'),
                original_date_evidence=registration.get('original_date_evidence'),
                published_at=role.get('published_at'),published_date=role.get('published_date'),
                publication_precision=role.get('publication_precision','unknown'),
                cover_report_date=role.get('cover_report_date'),original_publication_date=role.get('original_publication_date'),
                original_publication_precision=role.get('original_publication_precision'),
                source_title=role.get('source_title'),
                acquired_at=role.get('acquired_at'),extracted_at=role.get('extracted_at')))
        missing=[];external=[]
        for name in ('report.md','report.html','details.html'):
            path=stage/name
            if not path.is_file(): continue
            text=path.read_text(encoding='utf-8')
            links=re.findall(r'(?:\]\(|(?:src|href)=[\"\x27])([^\s\)\"\x27]+)',text)
            for raw in links:
                link=urlsplit(raw.strip('<>'))
                if link.scheme in {'http','https','data','mailto'} or raw.startswith('#'): continue
                if link.scheme or link.netloc or Path(unquote(link.path)).is_absolute():
                    external.append(raw);continue
                local=stage/unquote(link.path)
                if not local.resolve().is_relative_to(stage.resolve()) or not local.is_file():
                    missing.append(raw)
        package=dict(schema_version='stock-research-archive-package/1',result_id=result_id,
            security_id=security_id,source_asof=asof,exported_at=lab.now(),
            research_asof=result['research_asof'],generated_at=result['generated_at'],data_asof=result['data_asof'],
            files=files,source_originals=originals,source_original_gaps=gaps,
            missing_local_links=sorted(set(missing)),external_local_dependencies=sorted(set(external)),
            scope='Selected report, frozen metadata, declared attachments and explicitly referenced registered originals; unlisted sources are not copied')
        save_json(stage/'archive-package.json',package)
        return dict(files=len(files),source_originals=len(originals),source_original_gaps=gaps,missing_local_links=package['missing_local_links'],
                    external_local_dependencies=package['external_local_dependencies'])
    copied=publish(lab,target,copy)
    return dict(saved=True,output=str(target),result_id=result_id,**copied)


def run(args):
    lab=load_backend(args.lab_tool)
    if args.command in {'backup','verify','restore'}:
        backup=sibling('archive_backup')
        if args.command=='backup': return backup.backup(lab,Path(args.path),Path(args.output_dir))
        if args.command=='restore': return backup.restore(lab,Path(args.path),Path(args.output_dir))
        result=backup.verify_snapshot(lab,Path(args.path))
        return {key:value for key,value in result.items() if key!='manifest'}
    root=lab.lab_root(args.path)
    if args.command=='export':
        return package_result(lab,root,args.security,args.asof,args.result_id,args.output_dir)
    views=sibling('archive_views')
    if args.command=='view':
        document=views.build_view(lab,root,args.security,args.asof,module=args.module,
            date_from=args.date_from,date_to=args.date_to,query=args.query)
    else:
        document=views.compare(lab,root,args.security,args.asof,args.left,args.right)
    document['tool_provenance']=dict(schema=lab.SCHEMA,
        sha256=hashlib.sha256(Path(args.lab_tool).read_bytes()).hexdigest())
    target=output_path(lab,args.output_dir,root)
    files=publish(lab,target,lambda stage:views.render_view(document,stage))
    return dict(saved=True,output=str(target),kind=document['kind'],files=[Path(name).name for name in files],
                counts=document.get('counts',{}))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('view','compare','export','backup','verify','restore'):
        command=commands.add_parser(name)
        command.add_argument('path',help='Absolute private Lab root, or snapshot for verify/restore')
        command.add_argument('--lab-tool',required=True,help='Explicit absolute research_lab.py path')
        if name!='verify': command.add_argument('--output-dir',required=True)
        if name in {'view','compare','export'}:
            command.add_argument('--security',required=True)
            command.add_argument('--asof',required=True)
        if name=='view':
            command.add_argument('--module',choices=['fundamental','technical'])
            command.add_argument('--date-from');command.add_argument('--date-to');command.add_argument('--query')
        if name=='compare':
            command.add_argument('--left',required=True);command.add_argument('--right',required=True)
        if name=='export': command.add_argument('--result-id',required=True)
    args=parser.parse_args(argv)
    try:
        print(json.dumps(run(args),ensure_ascii=False,allow_nan=False));return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps(dict(saved=False,error_type=type(error).__name__,error=str(error)),ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
