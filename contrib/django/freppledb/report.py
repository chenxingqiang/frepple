#
# Copyright (C) 2007 by Johan De Taeye
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation; either version 2.1 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser
# General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA
#

# file : $URL$
# revision : $LastChangedRevision$  $LastChangedBy$
# date : $LastChangedDate$
# email : jdetaeye@users.sourceforge.net

from datetime import date, datetime
from xml.sax.saxutils import escape

from django.core.paginator import ObjectPaginator, InvalidPage
from django.shortcuts import render_to_response
from django.contrib.admin.views.decorators import staff_member_required
from django.template import RequestContext, loader
from django.db import connection
from django.http import Http404, HttpResponse
from django.conf import settings
from django.template import Library, Node, resolve_variable
from django.utils.encoding import smart_str
from django.utils.translation import ugettext as _

from freppledb.input.models import Plan
from freppledb.dbutils import python_date

# Parameter settings
ON_EACH_SIDE = 3       # Number of pages show left and right of the current page
ON_ENDS = 2            # Number of pages shown at the start and the end of the page list

# A variable to cache bucket information in memory
datelist = {}

def getBuckets(request, bucket=None, start=None, end=None):
  '''
  This function gets passed a name of a bucketization.
  It returns a list of buckets.
  The data are retrieved from the database table dates, and are
  stored in a python variable for performance
  '''
  global datelist
  # Pick up the arguments
  if not bucket:
    bucket = request.GET.get('bucket')
    if not bucket:
      try: bucket = request.user.get_profile().buckets
      except: bucket = 'default'
  if not start:
    start = request.GET.get('start')
    if start:
      try:
        (y,m,d) = start.split('-')
        start = date(int(y),int(m),int(d))
      except:
        try: start = request.user.get_profile().startdate
        except: pass
        if not start: start = Plan.objects.all()[0].currentdate.date()
    else:
      try: start = request.user.get_profile().startdate
      except: pass
      if not start: start = Plan.objects.all()[0].currentdate.date()
  if not end:
    end = request.GET.get('end')
    if end:
      try:
        (y,m,d) = end.split('-')
        end = date(int(y),int(m),int(d))
      except:
        try: end = request.user.get_profile().enddate
        except: pass
        if not end: end = date(2030,1,1)
    else:
      try: end = request.user.get_profile().enddate
      except: pass
      if not end: end = date(2030,1,1)

  # Check if the argument is valid
  if bucket not in ('default','day','week','month','quarter','year'):
    raise Http404, "bucket name %s not valid" % bucket

  # Pick up the buckets
  if not bucket in datelist:
    # Read the buckets from the database if the data isn't available yet
    cursor = connection.cursor()
    field = (bucket=='day' and 'day_start') or bucket
    cursor.execute('''
      select %s, min(day_start), max(day_start)
      from dates
      group by %s
      order by min(day_start)''' \
      % (connection.ops.quote_name(field),connection.ops.quote_name(field)))
    # Compute the data to store in memory
    datelist[bucket] = [{'name': i, 'start': python_date(j), 'end': python_date(k)} for i,j,k in cursor.fetchall()]

  # Filter based on the start and end date
  if start and end:
    res = filter(lambda b: b['start'] <= end and b['end'] >= start, datelist[bucket])
  elif end:
    res = filter(lambda b: b['start'] <= end, datelist[bucket])
  elif start:
    res = filter(lambda b: b['end'] >= start, datelist[bucket])
  else:
    res = datelist[bucket]
  return (bucket,start,end,res)


class Report(object):
  '''
  The base class for all reports.
  The parameter values defined here are used as defaults for all reports, but
  can be overwritten.
  '''
  # Points to templates to be used for different output formats
  template = {}
  # The title of the report. Used for the window title
  title = ''
  # The default number of entities to put on a page
  paginate_by = 25

  # The resultset that returns a list of entities that are to be
  # included in the report.
  basequeryset = None

  # Whether or not the breadcrumbs are reset when we open the report
  reset_crumbs = True


class ListReport(Report):
  # Row definitions
  # Possible attributes for a row field are:
  #   - filter:
  #     Specifies how a value in the search field affects the base query.
  #   - filter_size:
  #     Specifies the size of the search field.
  #     The default value is 10 characters.
  #   - order_by:
  #     Model field to user for the sorting.
  #     It defaults to the name of the field.
  #   - title:
  #     Name of the row that is displayed to the user.
  #     It defaults to the name of the field.
  #   - sort:
  #     Whether or not this column can be used for sorting or not.
  #     The default is true.
  rows = ()


class TableReport(Report):
  # Row definitions
  # Possible attributes for a row field are:
  #   - filter:
  #     Specifies how a value in the search field affects the base query.
  #   - filter_size:
  #     Specifies the size of the search field.
  #     The default value is 10 characters.
  #   - order_by:
  #     Model field to user for the sorting.
  #     It defaults to the name of the field.
  #   - title:
  #     Name of the row that is displayed to the user.
  #     It defaults to the name of the field.
  #   - sort:
  #     Whether or not this column can be used for sorting or not.
  #     The default is true.
  rows = ()

  # Cross definitions.
  # Possible attributes for a row field are:
  #   - title:
  #     Name of the cross that is displayed to the user.
  #     It defaults to the name of the field.
  #   - editable:
  #     True when the field is editable in the page.
  #     The default value is false.
  crosses = ()

  # Column definitions
  # Possible attributes for a row field are:
  #   - title:
  #     Name of the cross that is displayed to the user.
  #     It defaults to the name of the field.
  columns = ()


def _generate_csv(rep, qs):
  '''
  This is a generator function that iterates over the report data and
  returns the data row by row in CSV format.
  '''
  import csv
  import StringIO
  sf = StringIO.StringIO()
  writer = csv.writer(sf, quoting=csv.QUOTE_NONNUMERIC)

  # Write a header row
  fields = [ ('title' in s[1] and s[1]['title']) or s[0] for s in rep.rows ]
  try:
    fields.extend([ ('title' in s[1] and s[1]['title']) or s[0] for s in rep.columns ])
    fields.extend([ ('title' in s[1] and s[1]['title']) or s[0] for s in rep.crosses ])
  except:
    pass
  writer.writerow(fields)
  yield sf.getvalue()

  if issubclass(rep,ListReport):
    # A "list report": Iterate over all rows
    for row in qs:
      # Clear the return string buffer
      sf.truncate(0)
      # Build the return value
      fields = [ getattr(row,s[0]) for s in rep.rows ]
      # Return string
      writer.writerow(fields)
      yield sf.getvalue()
  elif issubclass(rep,TableReport):
    # A "table report": Iterate over all rows and columns
    for row in qs:
      for col in row:
        # Clear the return string buffer
        sf.truncate(0)
        # Build the return value
        fields = [ col[s[0]] for s in rep.rows ]
        fields.extend([ col[s[0]] for s in rep.columns ])
        fields.extend([ col[s[0]] for s in rep.crosses ])
        # Return string
        writer.writerow(fields)
        yield sf.getvalue()
  else:
    raise Http404('Unknown report type')


@staff_member_required
def view_report(request, entity=None, **args):
  '''
  This is a generic view for two types of reports:
    - List reports, showing a list of values are rows
    - Table reports, showing in addition values per time buckets as columns
  The following arguments can be passed to the view:
    - report:
      Points to a subclass of Report.
      This is a required attribute.
    - extra_context:
      An additional set of records added to the context for rendering the
      view.
  '''
  global ON_EACH_SIDE
  global ON_ENDS

  # Pick up the report class
  try: reportclass = args['report']
  except: raise Http404('Missing report parameter in url context')

  # Pick up the list of time buckets
  if issubclass(reportclass, TableReport):
    (bucket,start,end,bucketlist) = getBuckets(request)
  else:
    bucket = start = end = bucketlist = None
  type = request.GET.get('type','html')  # HTML or CSV output

  # Pick up the filter parameters from the url
  counter = reportclass.basequeryset
  fullhits = counter.count()
  if entity:
    # The url path specifies a single entity.
    # We ignore all other filters.
    counter = counter.filter(pk__exact=entity)
  else:
    # The url doesn't specify a single entity, but may specify filters
    # Convert url parameters into queryset filters.
    # This block of code is copied from the django admin code.
    qs_args = dict(request.GET.items())
    for i in ('o', 'p', 'type'):
      # Filter out arguments which we aren't filters
      if i in qs_args: del qs_args[i]
    for key, value in qs_args.items():
      # Ignore empty filter values
      if not value or len(value) == 0: del qs_args[key]
      elif not isinstance(key, str):
        # 'key' will be used as a keyword argument later, so Python
        # requires it to be a string.
        del qs_args[key]
        qs_args[smart_str(key)] = value
    counter = counter.filter(**qs_args)

  # Pick up the sort parameter from the url
  sortparam = request.GET.get('o','1a')
  try:
    if sortparam[0] == '1':
      if sortparam[1] == 'd':
        counter = counter.order_by('-%s' % (('order_by' in reportclass.rows[0][1] and reportclass.rows[0][1]['order_by']) or reportclass.rows[0][0]))
        sortsql = '1 desc'
      else:
        sortparam = '1a'
        counter = counter.order_by(('order_by' in reportclass.rows[0][1] and reportclass.rows[0][1]['order_by']) or reportclass.rows[0][0])
        sortsql = '1 asc'
    else:
      x = int(sortparam[0])
      if x > len(reportclass.rows) or x < 0:
        sortparam = '1a'
        counter = counter.order_by(('order_by' in reportclass.rows[0][1] and reportclass.rows[0][1]['order_by']) or reportclass.rows[0][0])
        sortsql = '1 asc'
      elif sortparam[1] == 'd':
        sortparm = '%dd' % x
        counter = counter.order_by(
          '-%s' % (('order_by' in reportclass.rows[x-1][1] and reportclass.rows[x-1][1]['order_by']) or reportclass.rows[x-1][0]),
          ('order_by' in reportclass.rows[0][1] and reportclass.rows[0][1]['order_by']) or reportclass.rows[0][0]
          )
        sortsql = '%d desc, 1 asc' % x
      else:
        sortparam = '%da' % x
        counter = counter.order_by(
          ('order_by' in reportclass.rows[x-1][1] and reportclass.rows[x-1][1]['order_by']) or reportclass.rows[x-1][0],
          ('order_by' in reportclass.rows[0][1] and reportclass.rows[0][1]['order_by']) or reportclass.rows[0][0]
          )
        sortsql = '%d asc, 1 asc' % x
  except:
    # A silent and safe exit in case of any exception
    sortparam = '1a'
    counter = counter.order_by(('order_by' in reportclass.rows[0][1] and reportclass.rows[0][1]['order_by']) or reportclass.rows[0][0])
    sortsql = '1 asc'

  # Build paginator
  if type == 'html':
    page = int(request.GET.get('p', '0'))
    paginator = ObjectPaginator(counter, reportclass.paginate_by)
    counter = counter[paginator.first_on_page(page)-1:paginator.first_on_page(page)-1+(reportclass.paginate_by or 0)]

  # Construct SQL statement, if the report has an SQL override method
  if hasattr(reportclass,'resultquery'):
    if settings.DATABASE_ENGINE == 'oracle':
      # Oracle
      basesql = counter._get_sql_clause(get_full_query=True)
      sql = basesql[3] or 'select %s %s' % (",".join(basesql[0]), basesql[1])
    elif settings.DATABASE_ENGINE == 'sqlite3':
      # SQLite
      basesql = counter._get_sql_clause()
      sql = 'select * %s' % basesql[1]
    else:
      # PostgreSQL and mySQL
      basesql = counter._get_sql_clause()
      sql = 'select %s %s' % (",".join(basesql[0]), basesql[1])
    sqlargs = basesql[2]

  # HTML output or CSV output?
  type = request.GET.get('type','html')
  if type == 'csv':
    # CSV output
    response = HttpResponse(mimetype='text/csv')
    response['Content-Disposition'] = 'attachment; filename=%s.csv' % reportclass.title.lower()
    if hasattr(reportclass,'resultquery'):
      # SQL override provided
      response._container = _generate_csv(reportclass, reportclass.resultquery(sql, sqlargs, bucket, start, end, sortsql=sortsql))
    else:
      # No SQL override provided
      response._container = _generate_csv(reportclass, counter)
    response._is_string = False
    return response

  # Create a copy of the request url parameters
  parameters = request.GET.copy()
  parameters.__setitem__('p', 0)

  # Calculate the content of the page
  if hasattr(reportclass,'resultquery'):
    # SQL override provided
    try:
      results = reportclass.resultquery(sql, sqlargs, bucket, start, end, sortsql=sortsql)
    except InvalidPage: raise Http404
  else:
    # No SQL override provided
    results = counter

  # If there are less than 10 pages, show them all
  page_htmls = []
  if paginator.pages <= 10:
    for n in range(0,paginator.pages):
      parameters.__setitem__('p', n)
      if n == page:
        page_htmls.append('<span class="this-page">%d</span>' % (page+1))
      else:
        page_htmls.append('<a href="%s?%s">%s</a>' % (request.path, escape(parameters.urlencode()),n+1))
  else:
      # Insert "smart" pagination links, so that there are always ON_ENDS
      # links at either end of the list of pages, and there are always
      # ON_EACH_SIDE links at either end of the "current page" link.
      if page <= (ON_ENDS + ON_EACH_SIDE):
          # 1 2 *3* 4 5 6 ... 99 100
          for n in range(0, page + max(ON_EACH_SIDE, ON_ENDS)+1):
            if n == page:
              page_htmls.append('<span class="this-page">%d</span>' % (page+1))
            else:
              parameters.__setitem__('p', n)
              page_htmls.append('<a href="%s?%s">%s</a>' % (request.path, escape(parameters.urlencode()),n+1))
          page_htmls.append('...')
          for n in range(paginator.pages - ON_EACH_SIDE, paginator.pages):
              parameters.__setitem__('p', n)
              page_htmls.append('<a href="%s?%s">%s</a>' % (request.path, escape(parameters.urlencode()),n+1))
      elif page >= (paginator.pages - ON_EACH_SIDE - ON_ENDS - 2):
          # 1 2 ... 95 96 97 *98* 99 100
          for n in range(0, ON_ENDS):
              parameters.__setitem__('p', n)
              page_htmls.append('<a href="%s?%s">%s</a>' % (request.path, escape(parameters.urlencode()),n+1))
          page_htmls.append('...')
          for n in range(page - max(ON_EACH_SIDE, ON_ENDS), paginator.pages):
            if n == page:
              page_htmls.append('<span class="this-page">%d</span>' % (page+1))
            else:
              parameters.__setitem__('p', n)
              page_htmls.append('<a href="%s?%s">%d</a>' % (request.path, escape(parameters.urlencode()),n+1))
      else:
          # 1 2 ... 45 46 47 *48* 49 50 51 ... 99 100
          for n in range(0, ON_ENDS):
              parameters.__setitem__('p', n)
              page_htmls.append('<a href="%s?%s">%d</a>' % (request.path, escape(parameters.urlencode()),n+1))
          page_htmls.append('...')
          for n in range(page - ON_EACH_SIDE, page + ON_EACH_SIDE + 1):
            if n == page:
              page_htmls.append('<span class="this-page">%s</span>' % (page+1))
            elif n == '.':
              page_htmls.append('...')
            else:
              parameters.__setitem__('p', n)
              page_htmls.append('<a href="%s?%s">%s</a>' % (request.path, escape(parameters.urlencode()),n+1))
          page_htmls.append('...')
          for n in range(paginator.pages - ON_ENDS - 1, paginator.pages):
              parameters.__setitem__('p', n)
              page_htmls.append('<a href="%s?%s">%d</a>' % (request.path, escape(parameters.urlencode()),n+1))

  # Prepare template context
  context = {
       'objectlist': results,
       'bucket': bucket,
       'startdate': start,
       'enddate': end,
       'paginator': paginator,
       'is_paginated': paginator.pages > 1,
       'has_next': paginator.has_next_page(page - 1),
       'has_previous': paginator.has_previous_page(page - 1),
       'current_page': page,
       'next_page': page + 1,
       'previous_page': page - 1,
       'pages': paginator.pages,
       'hits' : paginator.hits,
       'fullhits': fullhits,
       'page_htmls': page_htmls,
       # Never reset the breadcrumbs if an argument entity was passed.
       # Otherwise depend on the value in the report class.
       'reset_crumbs': reportclass.reset_crumbs and entity == None,
       'title': (entity and '%s %s %s' % (reportclass.title,_('for'),entity)) or reportclass.title,
       'rowheader': _create_rowheader(request, sortparam, reportclass),
       'crossheader': issubclass(reportclass, TableReport) and _create_crossheader(request, reportclass),
       'columnheader': issubclass(reportclass, TableReport) and _create_columnheader(request, reportclass, bucketlist),
     }
  if 'extra_context' in args: context.update(args['extra_context'])

  # Render the view
  return render_to_response(args['report'].template,
    context, context_instance=RequestContext(request))


def _create_columnheader(req, cls, bucketlist):
  '''
  Generate html header row for the columns of a table report.
  '''
  # @todo not very clean and consistent with cross and row
  return ' '.join(['<th>%s</th>' % j['name'] for j in bucketlist])


def _create_crossheader(req, cls):
  '''
  Generate html for the crosses of a table report.
  '''
  res = []
  for crs in cls.crosses:
    title = unicode((crs[1].has_key('title') and crs[1]['title']) or crs[0]).replace(' ','&nbsp;')
    # Editable crosses need to be a bit higher...
    if crs[1].has_key('editable'):
      if (callable(crs[1]['editable']) and crs[1]['editable'](req)) \
      or (not callable(crs[1]['editable']) and crs[1]['editable']):
        title = '<span style="line-height:18pt;">' + title + '</span>'
    res.append(title)
  return '<br/>'.join(res)


def _create_rowheader(req, sort, cls):
  '''
  Generate html header row for the columns of a table or list report.
  '''
  result = ['<form>']
  number = 0
  args = req.GET.copy()
  args2 = req.GET.copy()

  # A header cell for each row
  for row in cls.rows:
    number = number + 1
    title = unicode((row[1].has_key('title') and row[1]['title']) or row[0])
    if not row[1].has_key('sort') or row[1]['sort']:
      # Sorting is allowed
      if int(sort[0]) == number:
        if sort[1] == 'a':
          # Currently sorting in ascending order on this column
          args['o'] = '%dd' % number
          y = 'class="sorted ascending"'
        else:
          # Currently sorting in descending order on this column
          args['o'] = '%da' % number
          y = 'class="sorted descending"'
      else:
        # Sorted on another column
        args['o'] = '%da' % number
        y = ''
      if 'filter' in cls.rows[number-1][1]:
        result.append( '<th %s><a href="%s?%s">%s%s</a><br/><input type="text" size="%d" value="%s" name="%s" tabindex="%d"/></th>' \
          % (y, req.path, escape(args.urlencode()),
             title[0].upper(), title[1:],
             (row[1].has_key('filter_size') and row[1]['filter_size']) or 10,
             args.get(cls.rows[number-1][1]['filter'],''),
             cls.rows[number-1][1]['filter'], number+1000,
             ) )
        if cls.rows[number-1][1]['filter'] in args2: del args2[cls.rows[number-1][1]['filter']]
      else:
        result.append( '<th %s style="vertical-align:top"><a href="%s?%s">%s%s</a></th>' \
          % (y, req.path, escape(args.urlencode()),
             title[0].upper(), title[1:],
            ) )
        if row[0] in args2: del args2[row[0]]
    else:
      # No sorting is allowed on this field
        result.append( '<th style="vertical-align:top">%s%s</th>' \
          % (title[0].upper(), title[1:]) )

  # Extra hidden fields for query parameters that aren't rows
  for key in args2:
    result.append( '<th><input type="hidden" name="%s" value="%s"/>' % (key, args[key]))

  # 'Go' button
  result.append( '<th><input type="submit" value="Go" tabindex="1100"/></th></form>' )
  return '\n'.join(result)
